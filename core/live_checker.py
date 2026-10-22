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
        """从直播间链接 / 主页链接 / 纯数字中提取直播间号."""
        text = (text or "").strip()
        if text.isdigit():
            return text
        m = re.search(r"live\.douyin\.com/(\d+)", text)
        if m:
            return m.group(1)
        # 主页链接：/user/{sec_uid} 或短链 /{sec_uid}
        m = re.search(r"douyin\.com(?:/user)?/([A-Za-z0-9_-]{8,})", text)
        if m:
            return m.group(1)
        return ""

    async def check(self, room_id: str) -> LiveStatus:
        """检查一个直播间当前是否开播."""
        room_id = (room_id or "").strip()
        if not room_id:
            raise ValueError("直播间号为空")
        await self._ensure_browser()
        ctx = await self._new_context()
        try:
            page = await ctx.new_page()
            url = f"https://live.douyin.com/{room_id}/"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.warning("打开直播间页面失败 %s: %s", room_id, e)
            await page.wait_for_timeout(4000)
            info: dict = await page.evaluate(_EVAL_JS)
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
