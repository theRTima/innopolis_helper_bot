"""Ticket requests are explicit, redacted and deduplicated before API work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.bot import handlers
from app.storage.db import Database
from app.storage.models import Analitics, TicketJob
from app.tickets.payloads import improvement_payload
from app.tickets.service import request_support_ticket


async def test_support_request_is_queued_once_and_redacted(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    await db.init()
    try:
        question_id = await db.save_question(
            telegram_id=21,
            chat_id=21,
            question="Помогите +7 999 111-22-33, мне пишет @person",
            answer="Напишите на private@example.com",
            sources=[{"document": "Справка", "page": "3"}],
        )
        first, created = await request_support_ticket(
            db, question_id=question_id, telegram_id=21, provider="demo"
        )
        second, created_again = await request_support_ticket(
            db, question_id=question_id, telegram_id=21, provider="demo"
        )
        assert created and not created_again and first.id == second.id
        assert first.state == "queued"
        payload = first.payload
        assert "private@example.com" not in payload["description"]
        assert "@person" not in payload["description"]
        assert "999 111" not in payload["description"]
        assert "Справка, стр. 3" in payload["description"]
        async with db.session() as session:
            rows = (await session.scalars(select(TicketJob))).all()
            analytics = await session.scalar(
                select(Analitics).where(Analitics.question_id == question_id)
            )
        assert len(rows) == 1
        assert analytics.operator_status == "requested"
    finally:
        await db.close()


async def test_unresolved_feedback_does_not_create_ticket(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    await db.init()
    try:
        question_id = await db.save_question(
            telegram_id=5, chat_id=5, question="Где календарь?"
        )
        callback = SimpleNamespace(
            data=f"resolution:unresolved:{question_id}",
            from_user=SimpleNamespace(id=5),
            answer=AsyncMock(),
            message=None,
        )
        settings = SimpleNamespace(ticket_provider="demo")
        await handlers.handle_resolution(callback, db, settings)
        assert await db.get_ticket_for_question(question_id) is None
        assert "Создать заявку" in callback.answer.call_args.args[0]
    finally:
        await db.close()


async def test_explicit_operator_request_queues_ticket(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    await db.init()
    try:
        question_id = await db.save_question(
            telegram_id=8, chat_id=9, question="Не могу открыть фильтр"
        )
        message = SimpleNamespace(
            text="мне нужен оператор, пожалуйста",
            from_user=SimpleNamespace(id=8),
            chat=SimpleNamespace(id=9),
            answer=AsyncMock(),
        )
        settings = SimpleNamespace(ticket_provider="demo", support_url="")
        await handlers.handle_question(message, None, None, db, settings)
        assert await db.get_ticket_for_question(question_id) is None
        assert "Опишите проблему" in message.answer.call_args.args[0]
        message.text = "/operator Не могу открыть фильтр в БАРС-Офисе"
        await handlers.handle_operator_command(message, db, settings)
        latest = await db.latest_question_id(telegram_id=8, chat_id=9)
        assert latest != question_id
        job = await db.get_ticket_for_question(latest)
        assert job is not None and job.state == "queued"
        assert "принят" in message.answer.call_args.args[0]
        # An unanswered question should not inherit later unrelated chat text.
        await db.save_question(
            telegram_id=8, chat_id=9,
            question="Другой вопрос с secret@example.com",
        )
        context = await db.get_question_context(
            question_id=question_id, telegram_id=8
        )
        assert context["messages"] == []
    finally:
        await db.close()


def test_improvement_payload_separates_fact_and_hypothesis():
    payload = improvement_payload(
        area="bars_office",
        kind="error",
        issue="Фильтр !> 200",
        period="7 дней",
        questions=12,
        users=9,
        previous=4,
        unresolved=5,
        no_evidence=3,
        examples=["Не работает фильтр, пишите @private"],
        documents=["Инструкция БО"],
        hypothesis="Проверить описание оператора",
    )
    assert payload.local_key.startswith("improvement:")
    assert "Наблюдения" in payload.description
    assert "Гипотеза для проверки" in payload.description
    assert "@private" not in payload.description
