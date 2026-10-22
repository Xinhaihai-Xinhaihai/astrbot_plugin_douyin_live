"""订阅与转发群存储.

数据保存在插件数据目录的 douyin_live.json，结构：
{
  "subs":   {"<抖音号或直播间号>": {"nickname": "...", "room_id": "...", "live_state": 0, "last_notify": 0}},
  "groups": ["123456", "789012"],
}

live_state: 0=未知/未开播, 1=开播中

注意：全部用 asyncio（异步），save 用临时文件 + 原子替换，
不在事件循环里做同步阻塞文件 I/O，避免卡死。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SubscriptionStore:
    """异步订阅存储，写入用临时文件+原子替换，避免锁内阻塞 I/O 卡死事件循环。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._write_lock = asyncio.Lock()
        self.data: dict[str, Any] = {"subs": {}, "groups": []}
        self._load()

    # ---------- I/O ----------
    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.setdefault("subs", {})
                self.data.setdefault("groups", [])
            except Exception as e:
                logger.error("读取订阅数据失败: %s", e)

    async def save(self) -> None:
        """异步原子写入。所有写操作共用一个锁，串行化避免并发写坏文件。"""
        async with self._write_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                content = json.dumps(self.data, ensure_ascii=False, indent=2)
                # 写临时文件后原子替换，避免写入一半崩溃损坏数据
                fd, tmp = tempfile.mkstemp(
                    dir=str(self.path.parent), prefix=".douyin_live.", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(content)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.path)
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except Exception as e:
                logger.error("保存订阅数据失败: %s", e)

    # ---------- 订阅 ----------
    async def add_sub(self, key: str, nickname: str = "") -> bool:
        key = (key or "").strip()
        if not key:
            return False
        if key in self.data["subs"]:
            return False
        self.data["subs"][key] = {
            "nickname": nickname,
            "room_id": "",
            "live_state": 0,
            "last_notify": 0,
        }
        await self.save()
        return True

    async def remove_sub(self, key: str) -> bool:
        key = (key or "").strip()
        if key in self.data["subs"]:
            del self.data["subs"][key]
            await self.save()
            return True
        return False

    def get_sub(self, key: str) -> dict | None:
        key = (key or "").strip()
        return dict(self.data["subs"].get(key) or {})

    def all_subs(self) -> list[tuple[str, dict]]:
        return [(k, dict(v)) for k, v in self.data["subs"].items()]

    # ---------- 转发群 ----------
    async def add_group(self, gid: str) -> bool:
        gid = (gid or "").strip()
        if not gid:
            return False
        if gid in self.data["groups"]:
            return False
        self.data["groups"].append(gid)
        await self.save()
        return True

    async def remove_group(self, gid: str) -> bool:
        gid = (gid or "").strip()
        if gid in self.data["groups"]:
            self.data["groups"].remove(gid)
            await self.save()
            return True
        return False

    def all_groups(self) -> list[str]:
        return list(self.data["groups"])

    # ---------- 状态更新 ----------
    async def set_live_state(self, key: str, live: bool) -> bool:
        """更新开播状态，返回状态是否发生变化。"""
        key = (key or "").strip()
        sub = self.data["subs"].get(key)
        if not sub:
            return False
        old = int(sub.get("live_state", 0))
        new = 1 if live else 0
        if old == new:
            return False
        sub["live_state"] = new
        if live:
            sub["last_notify"] = time.time()
        await self.save()
        return True

    async def set_room_id(self, key: str, room_id: str) -> None:
        key = (key or "").strip()
        sub = self.data["subs"].get(key)
        if sub and room_id:
            sub["room_id"] = room_id
            await self.save()