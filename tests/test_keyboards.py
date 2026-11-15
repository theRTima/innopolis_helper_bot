from app.bot.keyboards import feedback_keyboard
from app.bot.handlers import _remove_answer_buttons
from aiogram.types import Message
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def test_feedback_keyboard_callbacks() -> None:
    markup = feedback_keyboard(42)
    buttons = markup.inline_keyboard[0]

    assert [button.text for button in buttons] == ["Вопрос решён: да", "Нет"]
    assert [button.callback_data for button in buttons] == [
        "resolution:resolved:42",
        "resolution:unresolved:42",
    ]


def test_keyboard_includes_source_link_and_feedback() -> None:
    markup = feedback_keyboard(
        7,
        [("Открыть источник [1] · стр. 2", "https://docs.example/documents/x")],
    )

    assert markup.inline_keyboard[0][0].url == "https://docs.example/documents/x"
    assert markup.inline_keyboard[1][0].callback_data == "resolution:resolved:7"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_link", [True, False])
async def test_removing_answer_buttons_preserves_links(monkeypatch, with_link):
    edit = AsyncMock()
    monkeypatch.setattr(Message, "edit_reply_markup", edit)
    links = [("Специалист", "https://example.com/help")] if with_link else []
    message = Message(message_id=1, date=datetime.now(timezone.utc),
                      chat={"id": 1, "type": "private"},
                      reply_markup=feedback_keyboard(7, links))
    await _remove_answer_buttons(SimpleNamespace(message=message))
    markup = edit.call_args.kwargs["reply_markup"]
    if with_link:
        assert markup.inline_keyboard[0][0].url == links[0][1]
        assert len(markup.inline_keyboard) == 1
    else:
        assert markup is None
