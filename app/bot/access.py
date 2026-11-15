"""Restrict Telegram messages and callbacks to configured usernames."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class UsernameWhitelistMiddleware(BaseMiddleware):
    def __init__(self, allowed_usernames: frozenset[str]) -> None:
        self.allowed_usernames = allowed_usernames

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        username = (getattr(user, "username", None) or "").lower()
        if username and username in self.allowed_usernames and not user.is_bot:
            return await handler(event, data)

        text = "Access is restricted. Contact the bot administrator."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
        return None
