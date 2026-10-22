"""订阅与转发群存储.

数据保存在插件数据目录的 douyin_live.json，结构：
{
  "subs":   {"<抖音号或直播间号>": {"nickname": "...", "room_id": "...", "live_state": 0, "last_notify": 0}},
  "groups": ["123456", "789012"],
}

live_state: 0=未知/未开播, 1=开播中
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SubscriptionStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {"subs": {}, "groups": []}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.setdefault("subs", {})
                self.data.setdefault("groups", [])
            except Exception as e:
                logger.error("读取订阅数据失败: %s", e)

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(self.data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.error("保存订阅数据失败: %s", e)

    # ---------- 订阅 ----------
    def add_sub(self, key: str, nickname: str = "") -> bool:
        key = (key or "").strip()
        if not key:
            return False
        with self._lock:
            if key in self.data["subs"]:
                return False
            self.data["subs"][key] = {
                "nickname": nickname,
                "room_id": "",
                "live_state": 0,
                "last_notify": 0,
            }
            self.save()
        return True

    def remove_sub(self, key: str) -> bool:
        key = (key or "").strip()
        with self._lock:
            if key in self.data["subs"]:
                del self.data["subs"][key]
                self.save()
                return True
        return False

    def get_sub(self, key: str) -> dict | None:
        key = (key or "").strip()
        with self._lock:
            return dict(self.data["subs"].get(key) or {})

    def all_subs(self) -> list[tuple[str, dict]]:
        with self._lock:
            return [(k, dict(v)) for k, v in self.data["subs"].items()]

    # ---------- 转发群 ----------
    def add_group(self, gid: str) -> bool:
        gid = (gid or "").strip()
        if not gid:
            return False
        with self._lock:
            if gid in self.data["groups"]:
                return False
            self.data["groups"].append(gid)
            self.save()
        return True

    def remove_group(self, gid: str) -> bool:
        gid = (gid or "").strip()
        with self._lock:
            if gid in self.data["groups"]:
                self.data["groups"].remove(gid)
                self.save()
                return True
        return False

    def all_groups(self) -> list[str]:
        with self._lock:
            return list(self.data["groups"])

    # ---------- 状态更新 ----------
    def set_live_state(self, key: str, live: bool) -> bool:
        """更新开播状态，返回状态是否发生变化."""
        key = (key or "").strip()
        with self._lock:
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
            self.save()
            return True

    def set_room_id(self, key: str, room_id: str) -> None:
        key = (key or "").strip()
        with self._lock:
            sub = self.data["subs"].get(key)
            if sub and room_id:
                sub["room_id"] = room_id
                self.save()
