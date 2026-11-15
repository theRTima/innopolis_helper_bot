from app.bot.keyboards import feedback_keyboard
from app.bot.handlers import _remove_answer_buttons, handle_sources
from app.storage.db import Database
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
    assert len(markup.inline_keyboard) == 1


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


@pytest.mark.asyncio
async def test_unresolved_answer_shows_ticket_button_after_feedback(monkeypatch):
    edit = AsyncMock()
    monkeypatch.setattr(Message, "edit_reply_markup", edit)
    link = ("Источник", "https://docs.example/source")
    message = Message(
        message_id=1, date=datetime.now(timezone.utc),
        chat={"id": 1, "type": "private"},
        reply_markup=feedback_keyboard(42, [link]),
    )

    await _remove_answer_buttons(
        SimpleNamespace(message=message), ticket_question_id=42
    )

    markup = edit.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == link[1]
    assert markup.inline_keyboard[1][0].callback_data == "ticket:create:42"


@pytest.mark.asyncio
async def test_source_button_reveals_links_and_keeps_other_buttons(tmp_path, monkeypatch):
    db = Database(tmp_path / "sources.db")
    await db.init()
    try:
        question_id = await db.save_question(
            telegram_id=7, chat_id=7, question="Где находится реестр мебели?"
        )
        await db.save_delivery(
            question_id=question_id, answer="В документе [1]",
            sources=[{
                "position": 1, "document": "Реестр мебели",
                "document_id": "furniture", "page": "2", "page_from": 2,
                "score": 1.0,
            }],
            status="with_sources", delivered=True,
        )
        edit = AsyncMock()
        monkeypatch.setattr(Message, "edit_reply_markup", edit)
        support = ("Специалист", "https://example.com/help")
        message = Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat={"id": 7, "type": "private"},
            reply_markup=feedback_keyboard(
                question_id, [support], has_sources=True
            ),
        )
        assert message.reply_markup.inline_keyboard[0][0].callback_data == (
            f"sources:{question_id}"
        )
        callback = SimpleNamespace(
            data=f"sources:{question_id}",
            from_user=SimpleNamespace(id=7),
            message=message, answer=AsyncMock(),
        )
        settings = SimpleNamespace(
            documents_base_url="https://docs.example",
            document_link_secret="secret",
        )
        await handle_sources(callback, db, settings)

        rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        assert rows[0][0].url.startswith("https://docs.example/")
        assert rows[1][0].callback_data == f"sources:hide:{question_id}"
        assert rows[1][0].text == "Назад"
        assert rows[2][0].url == support[1]
        assert rows[3][0].callback_data == f"resolution:resolved:{question_id}"
        callback.answer.assert_awaited_once_with()

        callback.data = f"sources:hide:{question_id}"
        callback.message = message.model_copy(update={
            "reply_markup": edit.call_args.kwargs["reply_markup"],
        })
        await handle_sources(callback, db, settings)

        hidden_rows = edit.call_args.kwargs["reply_markup"].inline_keyboard
        assert hidden_rows[0][0].callback_data == f"sources:{question_id}"
        assert hidden_rows[0][0].text == "Источники"
        assert hidden_rows[1][0].url == support[1]
        assert hidden_rows[2][0].callback_data == f"resolution:resolved:{question_id}"
        assert len(hidden_rows) == 3
    finally:
        await db.close()


def test_source_links_use_office_locators():
    from app.bot.handlers import _source_links
    settings = SimpleNamespace(documents_base_url="https://docs.example",
                               document_link_secret="secret")
    sources = [
        {"position": 1, "document_id": "word", "page": "1",
         "locator": {"kind": "docx-paragraph", "value": "12"}},
        {"position": 2, "document_id": "sheet", "page": "2",
         "locator": {"kind": "xlsx-range", "value": "Склад", "subvalue": "A2:C5"}},
        {"position": 3, "document_id": "slides", "page": "3",
         "locator": {"kind": "pptx-slide", "value": 3}},
        {"position": 4, "document_id": "office-pdf", "page": "4",
         "source_format": "docx", "locator": {"kind": "pdf-page", "value": 4}},
    ]
    links = _source_links(sources, settings)
    assert links[0][1].endswith("#paragraph=12")
    assert links[1][1].endswith("#sheet=%D0%A1%D0%BA%D0%BB%D0%B0%D0%B4&range=A2%3AC5")
    assert links[2][1].endswith("#slide=3")
    assert "view=rendered#page=4" in links[3][1]
