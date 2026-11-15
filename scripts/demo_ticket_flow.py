"""Show both ticket flows offline with an isolated disposable database."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import update

from app.analytics.report import (
    build_report,
    build_task_links,
    load_issue_tasks,
    load_records,
)
from app.storage.db import Database
from app.storage.models import Question, TicketJob
from app.tickets.payloads import improvement_payload
from app.tickets.providers import DemoTicketProvider
from app.tickets.service import request_support_ticket
from app.tickets.worker import TicketWorker


async def demo(db: Database) -> None:
    provider = DemoTicketProvider()
    worker = TicketWorker(db, provider)

    support_question = await db.save_question(
        telegram_id=101, chat_id=101,
        question="Как открыть производственный календарь?",
    )
    await db.save_delivery(
        question_id=support_question, answer="Надёжный ответ не найден",
        sources=[], status="no_evidence", delivered=True,
    )
    await db.save_resolution(
        question_id=support_question, telegram_id=101, value="unresolved"
    )
    support, _ = await request_support_ticket(
        db, question_id=support_question, telegram_id=101, provider="demo"
    )
    print(
        f"Вопрос #{support_question} → не решено → "
        f"запрос #{support.id} в очереди"
    )
    await worker.run_once()
    support = await db.get_ticket_for_question(support_question)
    print(
        f"Тикет {support.external_id} подтверждён demo-системой; "
        f"статус {support.external_status}"
    )

    group = ("bars_office", "navigation", "Поиск календаря")
    before_ids = []
    for user_id in (201, 202, 203):
        question_id = await db.save_question(
            telegram_id=user_id, chat_id=user_id,
            question="Не вижу календарь, напишите @private_user",
        )
        await db.save_delivery(
            question_id=question_id, answer="Ответ не найден",
            sources=[], status="no_evidence", delivered=True,
        )
        await db.save_resolution(
            question_id=question_id, telegram_id=user_id,
            value="unresolved",
        )
        before_ids.append(question_id)
    await db.save_question_labels(
        [(question_id, *group) for question_id in before_ids]
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    records = await load_records(
        db, since=now - timedelta(days=14), until=now,
        show_examples=True,
    )
    [signal] = build_report(records, days=7, end=now).signals
    print(
        f"Карточка «{signal.title}»: {signal.current.total} вопроса, "
        f"{signal.current.users} пользователя, "
        f"{signal.current.unresolved} не решены"
    )
    payload = improvement_payload(
        area=signal.area, kind=signal.kind, issue=signal.title,
        period="последние 7 дней", questions=signal.current.total,
        users=signal.current.users, previous=signal.previous,
        unresolved=signal.current.unresolved,
        no_evidence=signal.current.no_evidence,
        examples=signal.examples, documents=signal.documents,
        hypothesis=signal.action,
    )
    task, created = await db.enqueue_ticket(
        kind="improvement", provider="demo",
        dedupe_key=payload.local_key, payload=asdict(payload),
        issue_area=signal.area, issue_kind=signal.kind,
        issue_title=signal.title,
    )
    repeated, created_again = await db.enqueue_ticket(
        kind="improvement", provider="demo",
        dedupe_key=payload.local_key, payload=asdict(payload),
        issue_area=signal.area, issue_kind=signal.kind,
        issue_title=signal.title,
    )
    assert created and not created_again and task.id == repeated.id
    await worker.run_once()
    task = await db.get_ticket_for_issue(
        area=signal.area, kind=signal.kind, title=signal.title
    )
    print(f"Сотрудник подтвердил одну задачу {task.external_id}; дубля нет")

    provider.set_status(task.external_id, "closed")
    async with db.session() as session, session.begin():
        await session.execute(
            update(TicketJob).where(TicketJob.id == task.id).values(
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
    await worker.run_once()
    closed_at = now - timedelta(days=8)
    async with db.session() as session, session.begin():
        await session.execute(
            update(TicketJob).where(TicketJob.id == task.id).values(
                closed_at=closed_at
            )
        )
        await session.execute(
            update(Question).where(Question.id.in_(before_ids)).values(
                created_at=(now - timedelta(days=9)).isoformat()
            )
        )
    after_id = await db.save_question(
        telegram_id=204, chat_id=204,
        question="Где теперь производственный календарь?",
    )
    await db.save_delivery(
        question_id=after_id, answer="Откройте раздел «Календарь» [1]",
        sources=[{"document": "БАРС-Офис", "page": "4"}],
        status="with_sources", delivered=True,
    )
    await db.save_resolution(
        question_id=after_id, telegram_id=204, value="resolved"
    )
    await db.save_question_labels([(after_id, *group)])
    async with db.session() as session, session.begin():
        await session.execute(
            update(Question).where(Question.id == after_id).values(
                created_at=(now - timedelta(days=5)).isoformat()
            )
        )
    records = await load_records(
        db, since=now - timedelta(days=21), until=now,
        show_examples=False,
    )
    links = build_task_links(
        records, await load_issue_tasks(db), days=7, end=now,
        data_start=now - timedelta(days=21),
    )
    view = links[group]
    print(
        f"После закрытия {task.external_id}: "
        f"{view.before_total} → {view.after_total} вопросов, "
        f"{view.before_unresolved} → {view.after_unresolved} нерешённых "
        "за равные 7-дневные периоды"
    )


async def main() -> None:
    with TemporaryDirectory(prefix="iskra-ticket-demo-") as directory:
        db = Database(Path(directory) / "bot.sqlite3")
        await db.init()
        try:
            await demo(db)
        finally:
            await db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
