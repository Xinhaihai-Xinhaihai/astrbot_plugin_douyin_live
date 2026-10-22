"""抖音直播开播检测插件.

- 订阅：`订阅 <直播间号或主页链接>` / `取消订阅 <key>` / `订阅列表`
- 转发群：`直播转发 群号` / `取消转发 群号`
- 每 N 秒（默认 300）轮询全部订阅，开播时推送「封面+链接+配文」到所有转发群
- 群聊中说触发词（看看腿 等）时，立即检查并把正在播的主播发回去
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig

from .core.live_checker import DouyinLiveChecker, LiveStatus
from .core.store import SubscriptionStore


def _cfg_get(cfg: AstrBotConfig | None, key: str, default: Any) -> Any:
    try:
        v = cfg.get(key) if cfg is not None else None
        if v is None:
            return default
        return v
    except Exception:
        return default


class DouyinLivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.cfg = config
        self._store: SubscriptionStore | None = None
        self._checker: DouyinLiveChecker | None = None
        self._poll_task: asyncio.Task | None = None
        self._polling = False
        self._trigger_cd: dict[str, float] = {}
        self._platform_id: str = ""
        self._data_dir: Path | None = None
        self._reload_runtime()

    # ================= 配置 =================

    def _cfg_val(self, key: str, default: Any) -> Any:
        return _cfg_get(self.cfg, key, default)

    def _reload_runtime(self) -> None:
        """初始化/刷新 store 与 checker（配置变化时调用）."""
        self._data_dir = Path(StarTools.get_data_dir("astrbot_plugin_douyin_live"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._store = SubscriptionStore(self._data_dir / "douyin_live.json")
        self._checker = DouyinLiveChecker(
            ttwid=str(self._cfg_val("ttwid", "") or ""),
            browser_channel=str(self._cfg_val("browser_channel", "msedge") or ""),
        )

    def _remember_platform(self, event: AstrMessageEvent):
        try:
            self._platform_id = event.get_platform_id()
        except Exception:
            pass

    def _enabled(self) -> bool:
        return bool(self._cfg_val("enabled", True))

    def _trigger_words(self) -> list[str]:
        raw = self._cfg_val("trigger_words", [])
        if isinstance(raw, (list, tuple)):
            return [str(w).strip() for w in raw if str(w).strip()]
        # 兼容旧版逗号分隔字符串
        s = str(raw or "")
        return [w.strip() for w in s.replace("，", ",").split(",") if w.strip()]

    # ================= 生命周期 =================

    async def initialize(self):
        logger.info("[douyin_live] 插件初始化")
        if self._store is None:
            self._reload_runtime()
        await self._sync_from_config()
        self._start_polling()

    async def _sync_from_config(self) -> None:
        """把配置面板里订阅房间号 list / 转发群 list 同步进 store，并自动解析短链。"""
        if self._store is None:
            return
        # 订阅房间号（支持短链，自动解析）
        room_list = self._cfg_val("sub_room_ids", [])
        if isinstance(room_list, (list, tuple)):
            for item in room_list:
                item = str(item or "").strip()
                if not item:
                    continue
                room_id = DouyinLiveChecker.extract_room_id(item)
                if not room_id:
                    # 短链自动展开
                    import re
                    m = re.search(r"https?://v\.douyin\.com/[\w-]+", item)
                    if m:
                        room_id = await DouyinLiveChecker.resolve_short_link(m.group(0))
                if room_id:
                    await self._store.add_sub(room_id)
        # 转发群
        group_list = self._cfg_val("forward_groups", [])
        if isinstance(group_list, (list, tuple)):
            for gid in group_list:
                gid = str(gid or "").strip()
                if gid:
                    await self._store.add_group(gid)

    async def terminate(self):
        await self._stop_polling()
        try:
            if self._checker:
                await self._checker.close()
        except Exception:
            pass
        logger.info("[douyin_live] 插件已终止")

    def _start_polling(self):
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _stop_polling(self):
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None

    # ================= 轮询 =================

    async def _poll_loop(self):
        interval = max(60, int(self._cfg_val("poll_interval", 300) or 300))
        while True:
            try:
                await asyncio.sleep(interval)
                if not self._enabled() or self._polling:
                    continue
                self._polling = True
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[douyin_live] 轮询异常: %s", e)
            finally:
                self._polling = False

    async def _poll_once(self):
        subs = self._store.all_subs()
        groups = self._store.all_groups()
        if not subs:
            return
        for key, sub in subs:
            room_id = sub.get("room_id") or ""
            if not room_id:
                # 尝试从 key 里解析
                room_id = DouyinLiveChecker.extract_room_id(key)
                if room_id:
                    await self._store.set_room_id(key, room_id)
            if not room_id:
                continue
            try:
                status = await self._do_check(room_id)
            except Exception as e:
                logger.warning("[douyin_live] 检测 %s 失败: %s", room_id, e)
                continue
            changed = await self._store.set_live_state(key, status.live)
            if status.live and changed:
                logger.info("[douyin_live] %s(%s) 开播: %s", key, room_id, status.title[:30])
                if groups:
                    await self._broadcast_live(groups, key, status)

    async def _do_check(self, room_id: str) -> LiveStatus:
        assert self._checker is not None
        return await self._checker.check(room_id)

    # ================= 触发词监听 =================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_trigger(self, event: AstrMessageEvent):
        """监听触发词，即时检查并回复正在直播的主播"""
        if not self._enabled():
            return
        words = self._trigger_words()
        if not words:
            return
        msg = (event.get_message_str() or "").strip()
        if not msg:
            return
        # 冷却 10s，避免刷屏
        umo = event.unified_msg_origin
        now = time.time()
        if now - self._trigger_cd.get(umo, 0) < 10:
            return
        hit = next((w for w in words if w in msg), None)
        if not hit:
            return
        # 必须已有订阅
        subs = self._store.all_subs()
        if not subs:
            return
        hit = next((w for w in words if w in msg), None)
        if not hit:
            return
        # 过滤掉命令本身
        if msg.startswith("订阅") or msg.startswith("取消") or msg.startswith("直播转发"):
            return
        self._trigger_cd[umo] = now
        self._remember_platform(event)
        live_ones = []
        for key, sub in subs:
            room_id = sub.get("room_id") or DouyinLiveChecker.extract_room_id(key)
            if not room_id:
                continue
            try:
                status = await self._do_check(room_id)
            except Exception:
                continue
            if status.live:
                live_ones.append((key, status))
        if not live_ones:
            not_live = str(self._cfg_val("not_live_text", "") or "").strip()
            if not_live:
                yield event.make_result().message(not_live)
            return
        for key, status in live_ones:
            yield await self._build_live_result(status)

    # ================= 命令 =================

    @filter.command("订阅")
    async def sub_cmd(self, event: AstrMessageEvent, target: str = ""):
        """订阅 <直播间号/直播间链接/手机分享整段文本>。

        手机分享文本（含 v.douyin.com 短链）可直接整条粘进来，自动展开拿房间号。
        """
        import re

        self._remember_platform(event)
        msg = (event.get_message_str() or "").strip()
        short = re.search(r"https?://v\.douyin\.com/[\w-]+", msg)
        if short:
            room_id = await DouyinLiveChecker.resolve_short_link(short.group(0))
            if room_id:
                if await self._store.add_sub(room_id):
                    yield event.plain_result(f"订阅直播成功（房间号 {room_id}）")
                else:
                    yield event.plain_result(f"房间 {room_id} 已订阅过")
                return
            yield event.plain_result("分享链接解析失败，换个短链试试")
            return
        room_id = DouyinLiveChecker.extract_room_id((target or msg).strip())
        if not room_id:
            yield event.plain_result(
                "用法：订阅 <直播间号 或 直播间链接 或 手机分享整段文本>\n"
                "例：订阅 82685125012"
            )
            return
        if await self._store.add_sub(room_id):
            yield event.plain_result(f"订阅直播成功（直播间 {room_id}）")
        else:
            yield event.plain_result(f"直播间 {room_id} 已订阅过")

    @filter.command("取消订阅")
    async def unsub_cmd(self, event: AstrMessageEvent, target: str = ""):
        """取消订阅 <直播间号>"""
        target = (target or "").strip()
        room_id = DouyinLiveChecker.extract_room_id(target)
        if not room_id:
            yield event.plain_result("用法：取消订阅 <直播间号>")
            return
        if await self._store.remove_sub(room_id):
            yield event.plain_result(f"已取消订阅 {room_id}，不再提醒")
        else:
            yield event.plain_result(f"没有订阅 {room_id}")

    @filter.command("订阅列表")
    async def list_cmd(self, event: AstrMessageEvent):
        """查看当前订阅的全部直播间"""
        subs = self._store.all_subs()
        if not subs:
            yield event.plain_result("还没有订阅任何直播间")
            return
        lines = [f"{k} -> {'直播中' if v.get('live_state') else '未开播'}" for k, v in subs]
        yield event.plain_result("当前订阅：\n" + "\n".join(lines))

    @filter.command("添加触发词")
    async def add_trigger_word(self, event: AstrMessageEvent, word: str = ""):
        """添加触发词 <词>：逐个添加"""
        self._remember_platform(event)
        word = (word or "").strip()
        if not word:
            yield event.plain_result("用法：添加触发词 <词>")
            return
        words = self._trigger_words()
        if word in words:
            yield event.plain_result(f"触发词「{word}」已存在")
            return
        words.append(word)
        self.cfg["trigger_words"] = words
        self.cfg.save_config()
        yield event.plain_result(f"已添加触发词「{word}」，当前共 {len(words)} 个")

    @filter.command("删除触发词")
    async def del_trigger_word(self, event: AstrMessageEvent, word: str = ""):
        """删除触发词 <词>"""
        self._remember_platform(event)
        word = (word or "").strip()
        if not word:
            yield event.plain_result("用法：删除触发词 <词>")
            return
        words = self._trigger_words()
        if word not in words:
            yield event.plain_result(f"触发词「{word}」不存在")
            return
        words.remove(word)
        self.cfg["trigger_words"] = words
        self.cfg.save_config()
        yield event.plain_result(f"已删除触发词「{word}」，剩余：{'、'.join(words) if words else '无'}")

    @filter.command("触发词列表")
    async def trigger_list_cmd(self, event: AstrMessageEvent):
        """查看当前全部触发词"""
        words = self._trigger_words()
        if not words:
            yield event.plain_result("还没有配置触发词")
            return
        yield event.plain_result("触发词：" + "、".join(words))

    @filter.command("直播转发")
    async def forward_cmd(self, event: AstrMessageEvent, gid: str = ""):
        """直播转发 <群号>：把开播推送转发到该群"""
        self._remember_platform(event)
        gid = (gid or "").strip()
        if not gid:
            yield event.plain_result("用法：直播转发 <群号>")
            return
        if await self._store.add_group(gid):
            yield event.plain_result(f"已添加转发群 {gid}，开播时会推送到该群")
        else:
            yield event.plain_result(f"群 {gid} 已在转发列表")

    @filter.command("取消转发")
    async def unforward_cmd(self, event: AstrMessageEvent, gid: str = ""):
        """取消转发 <群号>"""
        gid = (gid or "").strip()
        if not gid:
            yield event.plain_result("用法：取消转发 <群号>")
            return
        if await self._store.remove_group(gid):
            yield event.plain_result(f"已移除转发群 {gid}")
        else:
            yield event.plain_result(f"群 {gid} 不在转发列表")

    @filter.command("转发列表")
    async def forward_list_cmd(self, event: AstrMessageEvent):
        """查看当前转发群列表"""
        groups = self._store.all_groups()
        if not groups:
            yield event.plain_result("还没有配置转发群")
            return
        yield event.plain_result("转发群：" + "、".join(groups))

    # ================= 推送 =================

    async def _broadcast_live(self, groups: list[str], key: str, status: LiveStatus):
        for gid in groups:
            try:
                await self._send_to_group(gid, await self._build_live_result(status))
            except Exception as e:
                logger.warning("[douyin_live] 推送到群 %s 失败: %s", gid, e)

    async def _send_to_group(self, gid: str, chain: MessageChain):
        """向指定群发送消息。session 格式: <platform_id>:GroupMessage:<群号>"""
        platform_id = getattr(self, "_platform_id", "") or "aiocqhttp"
        if platform_id == "aiocqhttp":
            try:
                for platform in self.context.platform_manager.platform_insts:
                    meta = platform.meta()
                    pid = getattr(meta, "id", "") or ""
                    name = getattr(meta, "name", "") or ""
                    if pid:
                        platform_id = str(pid)
                        break
                    if name:
                        platform_id = str(name)
                        break
            except Exception as e:
                logger.debug("[douyin_live] 获取平台ID失败: %s", e)
        session = f"{platform_id}:GroupMessage:{gid}"
        await self.context.send_message(session, chain)

    async def _build_live_result(self, status: LiveStatus) -> MessageChain:
        chain = MessageChain()
        if self._cfg_val("fetch_cover", True) and status.cover:
            chain.url_image(status.cover)
        reply = str(self._cfg_val("reply_text", "看吧色鬼！") or "")
        title = status.title or status.nickname or status.room_id
        chain.message(f"{reply}\n{title}\n{status.room_url}")
        return chain
