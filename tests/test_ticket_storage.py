"""Durable ticket queue, deduplication and migrations from both parents."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.storage.db import Database
from app.storage.models import (
    Analitics,
    EscalationEvent,
    Question,
    QuestionAnalytics,
    TicketJob,
)


def payload() -> dict:
    return {
        "title": "Фильтр не работает",
        "description": "Проверить синтаксис",
        "kind": "support",
    }


async def test_support_ticket_lifecycle_and_deduplication(tmp_path):
    db = Database(tmp_path / "tickets.db")
    await db.init()
    try:
        question_id = await db.save_question(
            telegram_id=11, chat_id=22, question="Как настроить фильтр?"
        )
        ticket, created = await db.enqueue_ticket(
            kind="support", provider="demo",
            dedupe_key=f"support:{question_id}",
            payload=payload(), question_id=question_id, telegram_id=11,
        )
        assert created and ticket.state == "queued"
        assert ticket.telegram_id == 11
        again, created = await db.enqueue_ticket(
            kind="support", provider="demo",
            dedupe_key=f"support:{question_id}",
            payload=payload(), question_id=question_id, telegram_id=11,
        )
        assert not created and again.id == ticket.id
        with pytest.raises(ValueError, match="Question not found"):
            await db.enqueue_ticket(
                kind="support", provider="demo", dedupe_key="wrong-user",
                payload=payload(), question_id=question_id, telegram_id=33,
            )
        another_question = await db.save_question(
            telegram_id=11, chat_id=22, question="Другой вопрос"
        )
        with pytest.raises(ValueError, match="another question"):
            await db.enqueue_ticket(
                kind="support", provider="demo",
                dedupe_key=f"support:{question_id}", payload=payload(),
                question_id=another_question, telegram_id=11,
            )

        claimed = await db.claim_next_ticket(provider="demo")
        assert claimed is not None and claimed.id == ticket.id
        assert claimed.attempts == 1 and claimed.state == "creating"
        assert await db.claim_next_ticket(provider="demo") is None
        linked = await db.mark_ticket_created(
            ticket_id=ticket.id, external_id="DEMO-1",
            url="https://tickets.example/1", status="open",
        )
        assert linked.state == "created" and linked.external_id == "DEMO-1"
        assert await db.list_pollable_tickets(provider="demo") == []
        due = datetime.now(UTC) + timedelta(minutes=6)
        assert [job.id for job in await db.list_pollable_tickets(
            provider="demo", now=due
        )] == [ticket.id]
        assert await db.list_pollable_tickets(provider="demo", now=due) == []
        closed = await db.update_ticket_status(
            ticket_id=ticket.id, status="closed", closed=True
        )
        assert closed.state == "closed" and closed.closed_at is not None
        async with db.session() as session:
            analytics = await session.scalar(
                select(Analitics).where(Analitics.question_id == question_id)
            )
            assert analytics is not None
            assert analytics.operator_status == "delivered"
            assert len((await session.scalars(select(TicketJob))).all()) == 1
    finally:
        await db.close()


async def test_recent_question_context_and_escalation_events(tmp_path):
    db = Database(tmp_path / "context.db")
    await db.init()
    try:
        recent = await db.save_question(
            telegram_id=11, chat_id=22, question="Где найти инструкцию?"
        )
        await db.save_delivery(
            question_id=recent,
            answer="Инструкция в базе знаний.",
            sources=[{
                "title": "Руководство", "url": "https://example.test/doc",
            }],
            status="with_sources",
            delivered=True,
        )
        await db.record_escalation(question_id=recent, status="offered")
        context = await db.get_question_context(
            question_id=recent, telegram_id=11
        )
        assert context["question_id"] == recent
        assert context["answer"] == "Инструкция в базе знаний."
        assert context["sources"][0]["title"] == "Руководство"
        assert [item["role"] for item in context["messages"]] == [
            "user", "assistant"
        ]
        assert "telegram_id" not in context
        with pytest.raises(ValueError, match="Question not found"):
            await db.get_question_context(question_id=recent, telegram_id=99)

        stale = await db.save_question(
            telegram_id=11, chat_id=22, question="Старый вопрос"
        )
        suppressed = await db.save_question(
            telegram_id=11, chat_id=22, question="Подавленный вопрос"
        )
        await db.suppress_delivery(question_id=suppressed)
        limited = await db.save_question(
            telegram_id=11, chat_id=22, question="Лимит"
        )
        await db.save_delivery(
            question_id=limited, answer="Превышен лимит", sources=[],
            status="error", delivered=False,
            delivery_status="rate_limited",
        )
        async with db.session() as session, session.begin():
            old = await session.scalar(
                select(Analitics).where(Analitics.question_id == stale)
            )
            assert old is not None
            old.received_at = datetime.now(UTC) - timedelta(hours=25)
        assert await db.latest_question_id(
            telegram_id=11, chat_id=22
        ) == recent
        async with db.session() as session:
            analytics = await session.scalar(
                select(Analitics).where(Analitics.question_id == recent)
            )
            events = (await session.scalars(
                select(EscalationEvent).where(
                    EscalationEvent.question_id == recent
                )
            )).all()
            assert analytics is not None and analytics.operator_status is None
            assert [event.status for event in events] == ["offered"]
        async with db.session() as session, session.begin():
            old = await session.scalar(
                select(Analitics).where(Analitics.question_id == recent)
            )
            assert old is not None
            old.received_at = datetime.now(UTC) - timedelta(hours=25)
        assert await db.latest_question_id(
            telegram_id=11, chat_id=22
        ) is None
    finally:
        await db.close()


async def test_retry_uncertain_and_issue_deduplication(tmp_path):
    db = Database(tmp_path / "retry.db")
    await db.init()
    try:
        values = dict(
            kind="improvement", provider="demo",
            dedupe_key="improvement:filter",
            payload={
                "title": "Исправить фильтр",
                "description": "Частые обращения",
            },
            issue_area="bars_office", issue_kind="error",
            issue_title="Фильтр !> 200",
        )
        task, created = await db.enqueue_ticket(**values)
        assert created
        duplicate, created = await db.enqueue_ticket(
            **{**values, "dedupe_key": "different"}
        )
        assert not created and duplicate.id == task.id
        claimed = await db.claim_next_ticket(provider="demo")
        assert claimed is not None and claimed.id == task.id
        retry = await db.mark_ticket_failure(
            ticket_id=task.id, error="503", retryable=True
        )
        assert retry.state == "retry" and retry.next_attempt_at is not None
        assert await db.claim_next_ticket(provider="demo") is None
        claimed = await db.claim_next_ticket(
            provider="demo", now=datetime.now(UTC) + timedelta(hours=2)
        )
        assert claimed is not None and claimed.attempts == 2
        uncertain = await db.mark_ticket_failure(
            ticket_id=task.id, error="timeout after send", retryable=True,
            uncertain=True,
        )
        assert uncertain.state == "uncertain"
        assert await db.claim_next_ticket(
            provider="demo", now=datetime.now(UTC) + timedelta(days=2)
        ) is None
    finally:
        await db.close()


async def _migrate_to(path: Path, revision: str) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: (
                    config.attributes.__setitem__("connection", sync),
                    command.upgrade(config, revision),
                )
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "source_head", ["4d9a1f2c3b70", "0002_question_analytics"]
)
async def test_upgrade_from_either_existing_head_preserves_questions(
    tmp_path, source_head
):
    path = tmp_path / f"{source_head}.db"
    await _migrate_to(path, source_head)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO questions "
            "(telegram_id, chat_id, question, answer, sources) "
            "VALUES (7, 8, 'existing question', 'answer', '[]')"
        )
    db = Database(path)
    await db.init()
    try:
        async with db.session() as session:
            assert await session.get(Question, 1) is not None
            labels = await session.execute(select(QuestionAnalytics))
            assert labels.all() == []
            assert (await session.execute(select(TicketJob))).all() == []
        with sqlite3.connect(path) as connection:
            versions = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
            config = Config(
                str(Path(__file__).resolve().parents[1] / "alembic.ini")
            )
            current_head = ScriptDirectory.from_config(
                config
            ).get_current_head()
            assert versions == [(current_head,)]
    finally:
        await db.close()
