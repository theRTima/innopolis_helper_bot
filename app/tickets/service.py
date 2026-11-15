"""Queue redacted ticket requests without calling an external API inline."""

from __future__ import annotations

from dataclasses import asdict

from app.storage.db import Database
from app.storage.models import TicketJob
from app.tickets.payloads import support_payload


async def request_support_ticket(
    db: Database, *, question_id: int, telegram_id: int, provider: str
) -> tuple[TicketJob, bool]:
    context = await db.get_question_context(
        question_id=question_id, telegram_id=telegram_id
    )
    payload = support_payload(context)
    job, created = await db.enqueue_ticket(
        kind="support",
        provider=provider,
        dedupe_key=payload.local_key,
        payload=asdict(payload),
        question_id=question_id,
        telegram_id=telegram_id,
    )
    return job, created
