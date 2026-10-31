"""轻量限流器：按会话 / 按天 计数，防止 LLM 与搜索成本失控。"""

from __future__ import annotations

import time
from collections import defaultdict


class RateLimiter:
    """二维限流：每个 key 每天最多 daily_max 次。

    用法：
        lim = RateLimiter()
        if lim.allow("group:123", daily_max=30):
            ...  # 消耗一次
    """

    def __init__(self) -> None:
        self._counts: dict[str, tuple[str, int]] = {}

    def allow(self, key: str, daily_max: int) -> bool:
        """判断是否允许。通过则计数 +1。"""
        if daily_max <= 0:
            return True
        today = time.strftime("%Y-%m-%d")
        day, n = self._counts.get(key, (today, 0))
        if day != today:
            day, n = today, 0
        if n >= daily_max:
            self._counts[key] = (day, n)
            return False
        self._counts[key] = (day, n + 1)
        return True

    def remaining(self, key: str, daily_max: int) -> int:
        if daily_max <= 0:
            return -1
        today = time.strftime("%Y-%m-%d")
        day, n = self._counts.get(key, (today, 0))
        if day != today:
            return daily_max
        return max(0, daily_max - n)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._counts.clear()
        else:
            self._counts.pop(key, None)


class MinuteWindow:
    """滑动分钟窗口，用于「每会话每分钟最多 N 次」。"""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str, per_min: int) -> bool:
        if per_min <= 0:
            return True
        now = time.time()
        hits = [t for t in self._hits[key] if now - t < 60]
        if len(hits) >= per_min:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def cleanup(self) -> None:
        """清理过期记录，避免内存增长。"""
        now = time.time()
        for k in list(self._hits):
            hits = [t for t in self._hits[k] if now - t < 60]
            if hits:
                self._hits[k] = hits
            else:
                self._hits.pop(k, None)
