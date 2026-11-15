"""Process durable ticket requests outside the Telegram reply path."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from aiogram import Bot
from dotenv import load_dotenv

from app.storage.db import Database
from app.storage.models import TicketJob
from app.tickets.providers import (
    TicketPayload,
    TicketProvider,
    TicketProviderError,
    TicketResult,
    get_provider_from_env,
)

logger = logging.getLogger(__name__)


class TicketNotifier(Protocol):
    async def created(self, job: TicketJob, result: TicketResult) -> None: ...

    async def failed(self, job: TicketJob, *, uncertain: bool) -> None: ...


class TelegramTicketNotifier:
    """Send ticket results to the requester when a bot token is configured."""

    def __init__(self, token: str) -> None:
        self.bot = Bot(token)

    async def created(self, job: TicketJob, result: TicketResult) -> None:
        if job.kind != "support" or job.telegram_id is None:
            return
        text = f"Заявка {result.external_id} создана. Статус: {result.status}."
        await self.bot.send_message(chat_id=job.telegram_id, text=text)

    async def failed(self, job: TicketJob, *, uncertain: bool) -> None:
        if job.kind != "support" or job.telegram_id is None:
            return
        if uncertain:
            text = (
                "Не удалось подтвердить создание заявки. Запрос проверяется; "
                "повторно отправлять его не нужно."
            )
        else:
            text = (
                "Не удалось создать заявку. Попробуйте позже или обратитесь "
                "к администратору поддержки."
            )
        await self.bot.send_message(chat_id=job.telegram_id, text=text)

    async def close(self) -> None:
        await self.bot.session.close()


@dataclass(frozen=True)
class WorkerResult:
    created: int = 0
    retrying: int = 0
    uncertain: int = 0
    failed: int = 0
    polled: int = 0
    closed: int = 0


class TicketWorker:
    def __init__(
        self,
        db: Database,
        provider: TicketProvider,
        *,
        notifier: TicketNotifier | None = None,
        create_limit: int = 10,
        poll_limit: int = 50,
    ) -> None:
        if not 1 <= create_limit <= 100 or not 1 <= poll_limit <= 500:
            raise ValueError("Invalid ticket worker batch limits")
        self.db = db
        self.provider = provider
        self.notifier = notifier
        self.create_limit = create_limit
        self.poll_limit = poll_limit

    async def _notify_created(
        self, job: TicketJob, result: TicketResult
    ) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.created(job, result)
        except Exception as error:
            logger.exception("Ticket creation notice could not be delivered")
            await self.db.record_ticket_notice(
                ticket_id=job.id, error=type(error).__name__
            )
        else:
            await self.db.record_ticket_notice(ticket_id=job.id, error=None)

    async def _notify_failed(
        self, job: TicketJob, *, uncertain: bool
    ) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.failed(job, uncertain=uncertain)
        except Exception:
            logger.exception("Ticket failure notice could not be delivered")

    async def run_once(self) -> WorkerResult:
        """Run bounded creation and polling batches with durable state."""
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        stale = await self.db.mark_stale_claims_uncertain(before=old)
        if stale:
            logger.warning("Marked %d stale ticket claims uncertain", stale)

        find_remote = getattr(self.provider, "find_by_local_key", None)
        if find_remote is not None:
            for job in await self.db.list_uncertain_tickets(
                provider=self.provider.name, limit=self.create_limit
            ):
                try:
                    found = await find_remote(job.dedupe_key)
                except Exception:
                    logger.exception("Could not reconcile ticket job %s", job.id)
                    continue
                if found is not None:
                    updated = await self.db.mark_ticket_created(
                        ticket_id=job.id, external_id=found.external_id,
                        url=found.url, status=found.status,
                    )
                    await self._notify_created(updated, found)

        created = retrying = uncertain = failed = polled = closed = 0
        for _ in range(self.create_limit):
            job = await self.db.claim_next_ticket(provider=self.provider.name)
            if job is None:
                break
            try:
                payload = TicketPayload(
                    title=job.payload["title"],
                    description=job.payload["description"],
                    kind=job.kind,
                    local_key=job.dedupe_key,
                )
            except (KeyError, TypeError, ValueError):
                updated = await self.db.mark_ticket_failure(
                    ticket_id=job.id,
                    error="Invalid queued ticket payload",
                    retryable=False,
                )
                failed += 1
                await self._notify_failed(updated, uncertain=False)
                continue
            try:
                result = await self.provider.create(payload)
            except TicketProviderError as error:
                updated = await self.db.mark_ticket_failure(
                    ticket_id=job.id,
                    error=str(error),
                    retryable=error.retryable,
                    uncertain=error.uncertain,
                )
                if error.uncertain:
                    uncertain += 1
                    await self._notify_failed(updated, uncertain=True)
                elif error.retryable:
                    retrying += 1
                else:
                    failed += 1
                    await self._notify_failed(updated, uncertain=False)
                continue
            except Exception as error:
                # The remote create call may have succeeded before this error.
                updated = await self.db.mark_ticket_failure(
                    ticket_id=job.id,
                    error=f"Unexpected provider error: {type(error).__name__}",
                    retryable=False,
                    uncertain=True,
                )
                uncertain += 1
                await self._notify_failed(updated, uncertain=True)
                continue
            updated = await self.db.mark_ticket_created(
                ticket_id=job.id,
                external_id=result.external_id,
                url=result.url,
                status=result.status,
            )
            created += 1
            await self._notify_created(updated, result)

        if self.notifier is not None:
            for job in await self.db.list_unnotified_tickets(limit=self.poll_limit):
                await self._notify_created(
                    job,
                    TicketResult(
                        external_id=job.external_id,
                        url=job.external_url or "",
                        status=job.external_status or job.state,
                    ),
                )

        jobs = await self.db.list_pollable_tickets(
            provider=self.provider.name, limit=self.poll_limit
        )
        for job in jobs:
            try:
                result = await self.provider.get_status(job.external_id)
            except TicketProviderError as error:
                logger.warning(
                    "Could not poll ticket %s (%s)",
                    job.id,
                    type(error).__name__,
                )
                continue
            except Exception:
                logger.exception("Could not poll ticket %s", job.id)
                continue
            if result.external_id != job.external_id:
                logger.error(
                    "Provider returned wrong ID for ticket %s", job.id
                )
                continue
            await self.db.update_ticket_status(
                ticket_id=job.id,
                status=result.status,
                closed=result.closed,
            )
            polled += 1
            closed += int(result.closed)
        return WorkerResult(
            created=created,
            retrying=retrying,
            uncertain=uncertain,
            failed=failed,
            polled=polled,
            closed=closed,
        )


async def run(*, watch_seconds: float = 0) -> int:
    load_dotenv()
    provider = get_provider_from_env()
    if provider is None:
        logger.warning(
            "TICKET_PROVIDER is not set; ticket worker is idle"
        )
        await asyncio.Event().wait()
    database = Database(
        os.getenv("DATABASE_URL", "").strip()
        or os.getenv("DATABASE_PATH", "data/bot.sqlite3")
    )
    notifier = None
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        notifier = TelegramTicketNotifier(token)
    await database.init()
    try:
        worker = TicketWorker(database, provider, notifier=notifier)
        while True:
            result = await worker.run_once()
            logger.info("Ticket worker: %s", result)
            if watch_seconds <= 0:
                return 0
            await asyncio.sleep(watch_seconds)
    finally:
        if notifier is not None:
            await notifier.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process queued support tickets"
    )
    parser.add_argument(
        "--watch", type=float, default=0, metavar="SECONDS",
        help="Repeat at this interval; default is one pass",
    )
    args = parser.parse_args()
    if args.watch < 0:
        parser.error("--watch must be nonnegative")
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(run(watch_seconds=args.watch)))


if __name__ == "__main__":
    main()
