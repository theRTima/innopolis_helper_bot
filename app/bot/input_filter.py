"""Conservative input checks before retrieval and model requests."""

import re
import time
from collections import deque


def valid_question(text: str, *, max_length: int = 4000) -> bool:
    text = text.strip()
    if not 2 <= len(text) <= max_length:
        return False
    letters = re.findall(r"[a-zа-яё0-9]", text, re.IGNORECASE)
    if len(letters) < 2:
        return False
    return len(letters) < 6 or len(set(char.casefold() for char in letters)) > 1


class RateLimiter:
    """Per-process rate limit; use shared storage for multiple bot instances."""

    def __init__(self, seconds: float = 3.0) -> None:
        self.seconds = seconds
        self._last: dict[tuple[int, int], float] = {}

    def allow(self, user_id: int, chat_id: int) -> bool:
        now = time.monotonic()
        self._last = {key: value for key, value in self._last.items()
                      if now - value < self.seconds}
        key = (user_id, chat_id)
        if key in self._last:
            return False
        self._last[key] = now
        return True


class SpamCooldown:
    """Count every message per user, including messages rejected by RateLimiter."""

    def __init__(self, limit: int = 8, window: float = 5.0, cooldown: float = 30.0) -> None:
        self.limit = limit
        self.window = window
        self.cooldown = cooldown
        self._events: dict[int, deque[float]] = {}
        self._blocked_until: dict[int, float] = {}

    def check(self, user_id: int) -> str:
        now = time.monotonic()
        for uid, until in list(self._blocked_until.items()):
            if until <= now:
                del self._blocked_until[uid]
        if user_id in self._blocked_until:
            return "blocked"
        for uid, events in list(self._events.items()):
            while events and now - events[0] >= self.window:
                events.popleft()
            if not events:
                del self._events[uid]
        events = self._events.setdefault(user_id, deque())
        events.append(now)
        if len(events) >= self.limit:
            del self._events[user_id]
            self._blocked_until[user_id] = now + self.cooldown
            return "triggered"
        return "allowed"
