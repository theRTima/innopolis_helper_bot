from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Message
from aiogram.types import Update

from app.bot.access import UsernameWhitelistMiddleware
from app.config import Settings


def test_whitelist_configuration(monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_ALLOWED_USERNAMES", " @Alice, BOB,alice, ,@@Carol,"
    )
    assert Settings.from_env().telegram_allowed_usernames == {
        "alice",
        "bob",
        "carol",
    }
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERNAMES")
    assert Settings.from_env().telegram_allowed_usernames == frozenset()


@pytest.mark.parametrize(
    "value", ["@", "*", "Alice Smith", "https://t.me/alice"]
)
def test_whitelist_rejects_invalid_entries(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERNAMES", value)
    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_USERNAMES"):
        Settings.from_env()


@pytest.mark.parametrize("event_type", [Message, CallbackQuery])
@pytest.mark.parametrize(
    "username,allowed,is_bot,permitted",
    [
        ("ALIce", {"alice"}, False, True),
        ("BOB", {"alice", "bob"}, False, True),
        ("mallory", {"alice"}, False, False),
        ("alice_other", {"alice"}, False, False),
        (None, {"alice"}, False, False),
        ("alice", set(), False, False),
        ("alice", {"alice"}, True, False),
    ],
)
async def test_whitelist_blocks_before_handler(
    event_type, username, allowed, is_bot, permitted
):
    event = MagicMock(spec=event_type)
    event.from_user = SimpleNamespace(username=username, is_bot=is_bot)
    event.answer = AsyncMock()
    handler = AsyncMock(return_value="handled")
    middleware = UsernameWhitelistMiddleware(frozenset(allowed))
    result = await middleware(handler, event, {})
    if permitted:
        assert result == "handled"
        handler.assert_awaited_once_with(event, {})
        event.answer.assert_not_awaited()
    else:
        assert result is None
        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        if event_type is CallbackQuery:
            assert event.answer.call_args.kwargs["show_alert"] is True


async def test_anonymous_message_is_denied():
    event = MagicMock(spec=Message)
    event.from_user = None
    event.answer = AsyncMock()
    handler = AsyncMock()
    await UsernameWhitelistMiddleware(frozenset({"alice"}))(handler, event, {})
    handler.assert_not_awaited()
    event.answer.assert_awaited_once()


@pytest.mark.parametrize("event_type", ["message", "callback_query"])
@pytest.mark.parametrize("username", ["alice", "mallory"])
async def test_dispatcher_checks_access_before_any_handler(
    event_type, username
):
    dispatcher = Dispatcher()
    whitelist = UsernameWhitelistMiddleware(frozenset({"alice"}))
    dispatcher.message.outer_middleware(whitelist)
    dispatcher.callback_query.outer_middleware(whitelist)
    handler = AsyncMock(return_value="handled")

    async def route(event):
        return await handler(event)

    dispatcher.message.register(route)
    dispatcher.callback_query.register(route)
    user = {
        "id": 42,
        "is_bot": False,
        "first_name": "Alice",
        "username": username,
    }
    payload = (
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 42, "type": "private"},
            "from": user,
            "text": "/start",
        }
        if event_type == "message"
        else {
            "id": "callback",
            "from": user,
            "chat_instance": "test",
            "data": "feedback:1:helpful",
        }
    )
    update = Update.model_validate({"update_id": 1, event_type: payload})
    bot = Bot("123456:TEST_TOKEN")
    bot.session.make_request = AsyncMock(return_value=True)
    try:
        await dispatcher.feed_update(bot, update)
        assert handler.await_count == (1 if username == "alice" else 0)
        assert bot.session.make_request.await_count == (
            0 if username == "alice" else 1
        )
    finally:
        await bot.session.close()
