"""Conservative input checks before retrieval and model requests."""

import re
import time
import logging
from collections import deque

logger = logging.getLogger(__name__)


def valid_question(text: str, *, max_length: int = 4000) -> bool:
    text = text.strip()
    if not 2 <= len(text) <= max_length:
        return False
    letters = re.findall(r"[a-zа-яё0-9]", text, re.IGNORECASE)
    if len(letters) < 2:
        return False
    compact = "".join(letters).casefold()
    keyboard_rows = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю")
    if len(compact) >= 6 and any(compact in row or compact in row[::-1] for row in keyboard_rows):
        return False
    # Strong lexical signals only: ordinary typos must still reach search.
    words = re.findall(r"[а-яё]+", text.casefold())
    for word in words:
        if len(word) < 5:
            continue
        if word.startswith(("ы", "ь", "ъ")):
            return False
        if word.endswith("вплд"):
            return False
        if re.search(r"[аоуэы]{2}[фв][ыи]", word):
            return False
    return len(letters) < 6 or len(set(char.casefold() for char in letters)) > 1


class GibberishMute:
    """Mute repeated invalid messages, resetting the streak after valid input."""

    def __init__(self, window: float = 300.0) -> None:
        self.window = window
        self._counts: dict[int, int] = {}
        self._last_invalid: dict[int, float] = {}
        self._strikes: dict[int, int] = {}
        self._muted_until: dict[int, float] = {}

    def is_muted(self, user_id: int) -> bool:
        until = self._muted_until.get(user_id, 0.0)
        if until > time.monotonic():
            return True
        self._muted_until.pop(user_id, None)
        return False

    def record_invalid(self, user_id: int) -> None:
        now = time.monotonic()
        if now - self._last_invalid.get(user_id, -float("inf")) > self.window:
            self._counts[user_id] = 0
        count = self._counts.get(user_id, 0) + 1
        self._counts[user_id] = count
        self._last_invalid[user_id] = now
        if count >= 10:
            strikes = self._strikes.get(user_id, 0) + 1
            self._strikes[user_id] = strikes
            duration = 600 if strikes == 1 else 1800 if strikes == 2 else 3600
            self._muted_until[user_id] = now + duration
            self._counts[user_id] = 0
            logger.info("Invalid-input mute: user_id=%s strike=%s duration=%s", user_id, strikes, duration)

    def record_valid(self, user_id: int) -> None:
        self._counts.pop(user_id, None)
        self._last_invalid.pop(user_id, None)


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

    def retry_after(self, user_id: int, chat_id: int) -> float:
        """Seconds left before this user/chat may send another question."""
        last = self._last.get((user_id, chat_id))
        if last is None:
            return 0.0
        return max(0.0, self.seconds - (time.monotonic() - last))


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
