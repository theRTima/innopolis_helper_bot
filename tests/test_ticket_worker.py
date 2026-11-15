"""Worker tests exercise the durable queue with a local provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from app.storage.db import Database
from app.storage.models import TicketJob
from app.tickets.providers import (
    DemoTicketProvider,
    TicketPayload,
    TicketProviderError,
    TicketResult,
)
from app.tickets.worker import TelegramTicketNotifier, TicketWorker


@dataclass
class RecordingNotifier:
    created_jobs: list[tuple[int, str]] = field(default_factory=list)
    failed_jobs: list[tuple[int, bool]] = field(default_factory=list)

    async def created(self, job: TicketJob, result: TicketResult) -> None:
        self.created_jobs.append((job.id, result.external_id))

    async def failed(self, job: TicketJob, *, uncertain: bool) -> None:
        self.failed_jobs.append((job.id, uncertain))


@dataclass
class FailsOnceNotifier(RecordingNotifier):
    failures_left: int = 1

    async def created(self, job: TicketJob, result: TicketResult) -> None:
        if self.failures_left:
            self.failures_left -= 1
            raise RuntimeError("Telegram unavailable")
        await super().created(job, result)


@dataclass
class FailingProvider:
    error: TicketProviderError
    name: str = "demo"
    creates: int = 0

    async def create(self, payload: TicketPayload) -> TicketResult:
        self.creates += 1
        raise self.error

    async def get_status(self, external_id: str) -> TicketResult:
        raise AssertionError("No external ticket should exist")


@pytest.mark.asyncio
async def test_telegram_notice_uses_requester_private_chat(monkeypatch):
    class FakeBot:
        def __init__(self, token: str) -> None:
            self.token = token
            self.messages: list[tuple[int, str]] = []

        async def send_message(self, *, chat_id: int, text: str) -> None:
            self.messages.append((chat_id, text))

    monkeypatch.setattr("app.tickets.worker.Bot", FakeBot)
    notifier = TelegramTicketNotifier("fake-token")
    job = SimpleNamespace(kind="support", telegram_id=123, chat_id=456)
    result = TicketResult("DEMO-1", "https://jira.example/DEMO-1", "new")
    await notifier.created(job, result)
    await notifier.failed(job, uncertain=True)
    assert [chat_id for chat_id, _ in notifier.bot.messages] == [123, 123]
    assert "DEMO-1" in notifier.bot.messages[0][1]
    assert result.url not in notifier.bot.messages[0][1]
    assert "повторно" in notifier.bot.messages[1][1]


async def _support_job(db: Database) -> TicketJob:
    question_id = await db.save_question(
        telegram_id=123, chat_id=456, question="Как настроить фильтр?"
    )
    job, created = await db.enqueue_ticket(
        kind="support",
        provider="demo",
        dedupe_key=f"support:{question_id}",
        payload={
            "title": "Помощь с фильтром",
            "description": "Вопрос 1: фильтр не работает",
            "kind": "support",
        },
        question_id=question_id,
        telegram_id=123,
    )
    assert created
    return job


@pytest.mark.asyncio
async def test_worker_creates_once_and_reports_private_ticket(tmp_path):
    db = Database(tmp_path / "worker.db")
    await db.init()
    try:
        job = await _support_job(db)
        provider = DemoTicketProvider()
        notifier = RecordingNotifier()
        worker = TicketWorker(db, provider, notifier=notifier)

        result = await worker.run_once()
        linked = await db.get_ticket_for_question(job.question_id)
        assert result.created == 1
        assert linked.state == "created"
        assert linked.external_id.startswith("DEMO-")
        assert linked.telegram_id == 123
        assert notifier.created_jobs == [(job.id, linked.external_id)]

        again = await worker.run_once()
        assert again.created == 0
        assert notifier.created_jobs == [(job.id, linked.external_id)]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_creation_notice_is_retried_without_new_ticket(tmp_path):
    db = Database(tmp_path / "notice.db")
    await db.init()
    try:
        job = await _support_job(db)
        notifier = FailsOnceNotifier()
        worker = TicketWorker(db, DemoTicketProvider(), notifier=notifier)
        assert (await worker.run_once()).created == 1
        linked = await db.get_ticket_for_question(job.question_id)
        assert linked.notice_attempts == 1 and linked.notice_sent_at is None
        async with db.session() as session, session.begin():
            stored = await session.get(TicketJob, job.id)
            stored.notice_next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert (await worker.run_once()).created == 0
        linked = await db.get_ticket_for_question(job.question_id)
        assert linked.notice_sent_at is not None
        assert notifier.created_jobs == [(job.id, linked.external_id)]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ambiguous_create_is_not_retried_or_marked_delivered(tmp_path):
    db = Database(tmp_path / "uncertain.db")
    await db.init()
    try:
        job = await _support_job(db)
        provider = FailingProvider(TicketProviderError(
            "Provider timed out", retryable=True, uncertain=True
        ))
        notifier = RecordingNotifier()
        worker = TicketWorker(db, provider, notifier=notifier)

        result = await worker.run_once()
        assert result.uncertain == 1
        assert provider.creates == 1
        assert notifier.failed_jobs == [(job.id, True)]
        uncertain = await db.get_ticket_for_question(job.question_id)
        assert uncertain.state == "uncertain"
        assert uncertain.external_id is None

        await worker.run_once()
        assert provider.creates == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_uncertain_ticket_links_existing_remote_request(tmp_path):
    db = Database(tmp_path / "reconcile.db")
    await db.init()
    try:
        job = await _support_job(db)
        claimed = await db.claim_next_ticket(provider="demo")
        assert claimed.id == job.id
        await db.mark_ticket_failure(
            ticket_id=job.id, error="response lost", retryable=True,
            uncertain=True,
        )
        async with db.session() as session, session.begin():
            stored = await session.get(TicketJob, job.id)
            stored.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        class ExistingProvider(FailingProvider):
            async def find_by_local_key(self, local_key: str) -> TicketResult:
                assert local_key == job.dedupe_key
                return TicketResult("HELP-42", "https://jira.example/HELP-42", "Open")

        provider = ExistingProvider(TicketProviderError("must not create"))
        notifier = RecordingNotifier()
        result = await TicketWorker(db, provider, notifier=notifier).run_once()
        linked = await db.get_ticket_for_question(job.question_id)
        assert result.created == 0 and provider.creates == 0
        assert linked.state == "created" and linked.external_id == "HELP-42"
        assert notifier.created_jobs == [(job.id, "HELP-42")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_worker_polls_status_and_closes_issue_task(tmp_path):
    db = Database(tmp_path / "poll.db")
    await db.init()
    try:
        job, created = await db.enqueue_ticket(
            kind="improvement",
            provider="demo",
            dedupe_key="improvement:filter",
            payload={
                "title": "Исправить фильтр",
                "description": "Повторяющиеся проблемы",
                "kind": "improvement",
            },
            issue_area="office",
            issue_kind="error",
            issue_title="Фильтр отклоняет ответ",
        )
        assert created
        provider = DemoTicketProvider()
        worker = TicketWorker(db, provider)
        assert (await worker.run_once()).created == 1
        linked = await db.get_ticket_by_dedupe_key(job.dedupe_key)
        provider.set_status(linked.external_id, "closed")
        async with db.session() as session, session.begin():
            await session.execute(
                update(TicketJob).where(TicketJob.id == job.id).values(
                    next_attempt_at=datetime.now(timezone.utc)
                    - timedelta(minutes=1)
                )
            )

        result = await worker.run_once()
        closed = await db.get_ticket_by_dedupe_key(job.dedupe_key)
        assert (result.polled, result.closed) == (1, 1)
        assert closed.state == "closed"
        assert closed.external_status == "closed"
        assert closed.closed_at is not None
    finally:
        await db.close()
