"""抖音直播开播检测.

用 Playwright 无头浏览器渲染 live.douyin.com 直播间页面判定开播状态：
- 开播：页面含「在线观众 / 本场点赞」或标题形如「xxx的抖音直播间 - 抖音直播」
- 未开播：页面含「未开播 / 暂未开播 / 直播回放」等关键词

抖音 web 接口（webcast enter）带签名校验，直接 HTTP 调用会被
"url doesn't match" 拒绝；渲染真实页面最稳，且能拿到 ttwid 之外的必要 cookie。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_EVAL_JS = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  const title = document.title || '';
  const offline = ['未开播','暂未开播','已结束','主播暂未','暂无直播','直播回放'].some(k => t.includes(k));
  const live = ['在线观众','本场点赞','本场观众','全部商品'].some(k => t.includes(k))
             || /的抖音直播间/.test(title);
  const imgs = [...document.querySelectorAll('img')]
     .filter(i => i.src && !i.src.startsWith('data:'))
     .map(i => ({src: i.src, w: i.naturalWidth || i.width || 0, h: i.naturalHeight || i.height || 0}));
  const covers = imgs.filter(i => (i.w * i.h) >= (100 * 100));
  // 封面排序：优先 webcast 图床，其次面积大的
  covers.sort((a, b) => {
    const ap = a.src.includes('webcast') ? 0 : 1, bp = b.src.includes('webcast') ? 0 : 1;
    if (ap !== bp) return ap - bp;
    return b.w * b.h - a.w * a.h;
  });
  const nickM = title.match(/^(.{1,30}?)的抖音直播间/);
  // 直播标题：页面主标题区
  let liveTitle = '';
  const candidates = document.querySelectorAll('[class*="live-title"], [class*="title"]');
  for (const el of candidates) {
    const s = (el.innerText || '').trim();
    if (s && s.length > 3 && s.length < 60 && !s.includes('观众')) { liveTitle = s; break; }
  }
  return {
    url: location.href,
    title: title,
    offline: offline,
    live: live,
    covers: covers.slice(0, 5).map(c => c.src),
    nickname: nickM ? nickM[1] : '',
    liveTitle: liveTitle
  };
}
"""


@dataclass
class LiveStatus:
    room_id: str
    live: bool
    title: str = ""
    nickname: str = ""
    cover: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def room_url(self) -> str:
        return f"https://live.douyin.com/{self.room_id}/"


class DouyinLiveChecker:
    """基于 Playwright 的抖音直播间状态检查器."""

    def __init__(self, ttwid: str = "", browser_channel: str = "msedge") -> None:
        self.ttwid = (ttwid or "").strip()
        self.browser_channel = (browser_channel or "").strip()
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def _ensure_browser(self):
        if self._browser is not None and not self._closed:
            return
        async with self._lock:
            if self._browser is not None and not self._closed:
                return
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--mute-audio",
                ],
            }
            if self.browser_channel:
                launch_kwargs["channel"] = self.browser_channel
            try:
                self._browser = await self._pw.chromium.launch(**launch_kwargs)
            except Exception:
                self._browser = await self._pw.chromium.launch(
                    {k: v for k, v in launch_kwargs.items() if k != "channel"}
                )

    async def _new_context(self):
        ctx = await self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        if self.ttwid:
            await ctx.add_cookies(
                [
                    {
                        "name": "ttwid",
                        "value": self.ttwid,
                        "domain": ".douyin.com",
                        "path": "/",
                        "secure": True,
                    }
                ]
            )
        return ctx

    @staticmethod
    def extract_room_id(text: str) -> str:
        """从直播间链接 / 分享短链 / 纯数字中提取房间号."""
        text = (text or "").strip()
        if text.isdigit():
            return text
        # 手机分享短链展开后：webcast.amemv.com/douyin/webcast/reflow/{19位}
        m = re.search(r"webcast\.amemv\.com/douyin/webcast/reflow/(\d+)", text)
        if m:
            return m.group(1)
        m = re.search(r"live\.douyin\.com/(\d+)", text)
        if m:
            return m.group(1)
        # 兜底：任意 19 位数字（抖音房间 id）
        m = re.search(r"(?<!\d)(\d{19})(?!\d)", text)
        if m:
            return m.group(1)
        # 主页链接：/user/{sec_uid} 或短链 /{sec_uid}
        m = re.search(r"douyin\.com(?:/user)?/([A-Za-z0-9_-]{8,})", text)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    async def resolve_short_link(short_url: str) -> str:
        """把 v.douyin.com 短链解析成房间号（HTTP 跟随重定向，无需浏览器）."""
        import aiohttp

        short_url = (short_url or "").strip()
        m = re.search(r"https?://v\.douyin\.com/[\w-]+", short_url)
        if not m:
            return ""
        url = m.group(0)
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    final = str(r.url)
            return DouyinLiveChecker.extract_room_id(final)
        except Exception as e:
            logger.warning("短链解析失败 %s: %s", url, e)
            return ""

    async def check(self, room_id: str) -> LiveStatus:
        """检查一个房间当前是否开播.

        - 19 位房间 id（手机分享链接解析出来的）→ 走 reflow 页面 HTTP 检测，
          无需浏览器，快且稳。
        - 其他（live.douyin.com 的 12 位左右直播间号）→ 浏览器渲染检测。
        """
        room_id = (room_id or "").strip()
        if not room_id:
            raise ValueError("房间号为空")
        if room_id.isdigit() and len(room_id) >= 18:
            return await self._check_reflow_http(room_id)
        return await self._check_browser(room_id)

    async def _check_reflow_http(self, room_id: str) -> LiveStatus:
        """HTTP 拉 reflow 分享页判断开播（仅适用于 19 位房间 id）."""
        import aiohttp

        url = f"https://webcast.amemv.com/douyin/webcast/reflow/{room_id}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Cookie": f"ttwid={self.ttwid}" if self.ttwid else "",
                        "Referer": "https://live.douyin.com/",
                    },
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    text = await r.text()
        except Exception as e:
            logger.warning("reflow 请求失败 %s: %s", room_id, e)
            return LiveStatus(room_id=room_id, live=False, raw={"error": str(e)})

        live = ("直播中" in text) or ("观看直播" in text)
        offline = any(k in text for k in ("未开播", "已结束", "直播回放", "主播暂未"))
        cover_m = re.search(
            r'(https?://[^"\\\s]+?douyinpic[^"\\\s]*?/(?:720x720|500x500|400x400)[^"\\\s]*)',
            text,
        )
        return LiveStatus(
            room_id=room_id,
            live=live and not offline,
            title="",
            nickname="",
            cover=cover_m.group(1) if cover_m else "",
            raw={"method": "reflow_http", "len": len(text)},
        )

    async def _check_browser(self, room_id: str) -> LiveStatus:
        """浏览器渲染 live.douyin.com/{room_id}/ 判断开播（含整体超时保护，避免卡死事件循环）."""
        try:
            return await asyncio.wait_for(
                self._check_browser_inner(room_id),
                timeout=45,
            )
        except asyncio.TimeoutError:
            logger.warning("浏览器检查直播间 %s 超时", room_id)
            return LiveStatus(
                room_id=room_id,
                live=False,
                raw={"error": "timeout"},
            )

    async def _check_browser_inner(self, room_id: str) -> LiveStatus:
        await self._ensure_browser()
        ctx = await self._new_context()
        info: dict = {}
        try:
            page = await ctx.new_page()
            url = f"https://live.douyin.com/{room_id}/"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                logger.warning("打开直播间页面失败 %s: %s", room_id, e)
            try:
                await page.wait_for_timeout(4000)
            except Exception:
                pass
            try:
                info = await page.evaluate(_EVAL_JS)
            except Exception as e:
                logger.warning("执行直播间页面 JS 失败 %s: %s", room_id, e)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

        live = bool(info.get("live")) and not bool(info.get("offline"))
        return LiveStatus(
            room_id=room_id,
            live=live,
            title=info.get("liveTitle") or info.get("title") or "",
            nickname=info.get("nickname") or "",
            cover=(info.get("covers") or [""])[0] if info.get("covers") else "",
            raw=info,
        )

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            try:
                if self._browser:
                    await self._browser.close()
            except Exception:
                pass
            try:
                if self._pw:
                    await self._pw.stop()
            except Exception:
                pass
            self._browser = None
            self._pw = None
