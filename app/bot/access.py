"""Restrict Telegram messages and callbacks to configured usernames."""

import logging
import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class UsernameWhitelistMiddleware(BaseMiddleware):
    def __init__(
        self,
        allowed_usernames: frozenset[str] = frozenset(),
        invite_notify_chat_id: int | None = None,
    ) -> None:
        self.allowed_usernames = allowed_usernames
        self.invite_notify_chat_id = invite_notify_chat_id
        if allowed_usernames:
            logger.info(
                "Static whitelist loaded with %d username(s): %s",
                len(allowed_usernames),
                ", ".join(sorted(allowed_usernames)),
            )
        else:
            logger.info("Whitelist access will be checked in the database")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        username = (getattr(user, "username", None) or "").lower()
        database = data.get("db")
        if database is not None:
            permitted = False
            if user is not None and isinstance(event, Message):
                match = re.fullmatch(
                    r"/start(?:@\w+)?\s+invite_([A-Za-z0-9_-]{20,64})",
                    (event.text or "").strip(),
                    flags=re.IGNORECASE,
                )
                if match:
                    permitted = await database.accept_access_invite(
                        token=match.group(1),
                        telegram_id=user.id,
                        username=username or None,
                    )
                    if permitted:
                        logger.info(
                            "Access invite accepted: telegram_id=%s username=%r",
                            user.id,
                            username or None,
                        )
                        if self.invite_notify_chat_id is not None:
                            label = f"@{username}" if username else "no username"
                            try:
                                await event.bot.send_message(
                                    self.invite_notify_chat_id,
                                    f"Invite activated: Telegram user {label}, ID {user.id}.",
                                )
                            except Exception:
                                logger.warning(
                                    "Could not notify admin about invite activation",
                                    exc_info=True,
                                )
            if not permitted and user is not None:
                permitted = await database.is_user_allowed(
                    telegram_id=user.id,
                    username=username or None,
                )
        else:
            # Keep direct middleware use simple for isolated handler tests.
            permitted = bool(username) and username in self.allowed_usernames
        if permitted and not user.is_bot:
            return await handler(event, data)

        text = "Access is restricted. Contact the bot administrator."
        logger.info(
            "Denied access: username=%r user_id=%s event=%s",
            username or None,
            getattr(user, "id", None),
            type(event).__name__,
        )
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
        return None
