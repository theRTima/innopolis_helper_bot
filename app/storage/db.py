"""Async SQLAlchemy storage with Alembic migrations."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.storage.models import (
    AccessControlState,
    AccessInvite,
    AccessInviteUse,
    AllowedTelegramUser,
    Analitics,
    Answer,
    Conversation,
    EscalationEvent,
    Feedback,
    Message,
    Question,
    QuestionAnalytics,
    TicketJob,
    User,
)


class Database:
    def __init__(self, database: Path | str) -> None:
        if isinstance(database, Path) or "://" not in database:
            path = Path(database).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            self.url = f"sqlite+aiosqlite:///{path}"
        else:
            self.url = database
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    async def init(self, *, migrate: bool = True) -> None:
        if self._engine is not None:
            return
        engine = create_async_engine(self.url)
        if migrate:
            config = Config(
                str(Path(__file__).resolve().parents[2] / "alembic.ini")
            )
            try:
                async with engine.begin() as connection:
                    def upgrade(sync_connection) -> None:
                        config.attributes["connection"] = sync_connection
                        command.upgrade(config, "head")

                    await connection.run_sync(upgrade)
            except Exception:
                await engine.dispose()
                raise
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None

    def session(self) -> AsyncSession:
        if self._sessionmaker is None:
            raise RuntimeError("База данных не инициализирована")
        return self._sessionmaker()

    def _insert(self, model, values: dict):
        if self._engine is None:
            raise RuntimeError("База данных не инициализирована")
        if self._engine.dialect.name == "postgresql":
            return postgres_insert(model).values(**values)
        return sqlite_insert(model).values(**values)

    async def seed_allowed_usernames(self, usernames: frozenset[str]) -> None:
        """Import the legacy .env allowlist once, then let the database own it."""
        statement = self._insert(AccessControlState, {"id": 1}).on_conflict_do_nothing(
            index_elements=[AccessControlState.id]
        )
        async with self.session() as session, session.begin():
            inserted = await session.execute(statement)
            if inserted.rowcount:
                session.add_all(
                    AllowedTelegramUser(username=name, is_active=True)
                    for name in sorted(usernames)
                )

    async def is_user_allowed(self, telegram_id: int, username: str | None) -> bool:
        normalized = (username or "").strip().lstrip("@").lower() or None
        async with self.session() as session:
            user = await session.scalar(
                select(AllowedTelegramUser).where(
                    AllowedTelegramUser.telegram_id == telegram_id
                )
            )
            if user is not None:
                return user.is_active
            if normalized is None:
                return False
            user = await session.scalar(
                select(AllowedTelegramUser).where(
                    AllowedTelegramUser.username == normalized
                )
            )
            if user is None or not user.is_active:
                return False
            if user.telegram_id is not None:
                return False
            user.telegram_id = telegram_id
            await session.commit()
            return True

    async def accept_access_invite(
        self, *, token: str, telegram_id: int, username: str | None
    ) -> bool:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        normalized = (username or "").strip().lstrip("@").lower() or None
        async with self.session() as session, session.begin():
            invite = await session.scalar(
                select(AccessInvite)
                .where(AccessInvite.token_hash == token_hash)
                .with_for_update()
            )
            now = datetime.now(timezone.utc)
            if invite is None or invite.revoked_at is not None:
                return False
            expires_at = invite.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now or invite.use_count >= invite.max_uses:
                return False
            previously_used = await session.scalar(
                select(AccessInviteUse.id).where(
                    AccessInviteUse.invite_id == invite.id,
                    AccessInviteUse.telegram_id == telegram_id,
                )
            )
            if previously_used is not None:
                return False

            user = await session.scalar(
                select(AllowedTelegramUser).where(
                    AllowedTelegramUser.telegram_id == telegram_id
                )
            )
            if user is None and normalized:
                user = await session.scalar(
                    select(AllowedTelegramUser).where(
                        AllowedTelegramUser.username == normalized
                    )
                )
                if user is not None and user.telegram_id not in (None, telegram_id):
                    user = None
                    normalized = None
            if user is None:
                user = AllowedTelegramUser(
                    username=normalized,
                    telegram_id=telegram_id,
                    is_active=True,
                )
                session.add(user)
            else:
                user.is_active = True
                if user.telegram_id is None:
                    user.telegram_id = telegram_id

            session.add(
                AccessInviteUse(
                    invite_id=invite.id,
                    telegram_id=telegram_id,
                    username=normalized,
                )
            )
            invite.use_count += 1
        return True

    async def save_question(
        self,
        *,
        telegram_id: int,
        chat_id: int,
        question: str,
        answer: str | None = None,
        sources: list[dict[str, object]] | None = None,
        answer_status: str = "generated",
    ) -> int:
        row = Question(
            telegram_id=telegram_id,
            chat_id=chat_id,
            question=question,
            answer=answer or "",
            sources=json.dumps(sources or [], ensure_ascii=False),
        )
        async with self.session() as session:
            async with session.begin():
                user_insert = self._insert(User, {"telegram_id": telegram_id})
                await session.execute(user_insert.on_conflict_do_nothing(index_elements=[User.telegram_id]))
                user_id = await session.scalar(select(User.id).where(User.telegram_id == telegram_id))

                conversation_insert = self._insert(
                    Conversation, {"user_id": user_id, "chat_id": chat_id}
                )
                await session.execute(
                    conversation_insert.on_conflict_do_nothing(
                        index_elements=[Conversation.user_id, Conversation.chat_id]
                    )
                )
                conversation_id = await session.scalar(
                    select(Conversation.id).where(
                        Conversation.user_id == user_id, Conversation.chat_id == chat_id
                    )
                )

                session.add(row)
                await session.flush()
                session.add(Message(conversation_id=conversation_id, role="user", body=question))
                session.add(Analitics(
                    question_id=row.id,
                    conversation_id=conversation_id,
                    received_at=datetime.now(timezone.utc),
                    answer_status=answer_status if answer_status != "generated" else None,
                ))
                if answer is not None:
                    reply = Message(conversation_id=conversation_id, role="assistant", body=answer)
                    session.add(reply)
                    await session.flush()
                    session.add(Answer(message_id=reply.id, question_id=row.id, text=answer, status=answer_status))
            return row.id

    async def save_delivery(self, *, question_id: int, answer: str, sources: list,
                            status: str, delivered: bool,
                            delivery_status: str | None = None) -> None:
        if status not in {"with_sources", "no_evidence", "unverified", "error"}:
            raise ValueError("Invalid answer status")
        if delivery_status not in {None, "delivered", "failed", "rate_limited"}:
            raise ValueError("Invalid delivery status")
        async with self.session() as session, session.begin():
            question = await session.get(Question, question_id)
            analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
            if question is None or analytics is None:
                raise ValueError("Question not found")
            question.answer = answer
            question.sources = json.dumps(sources, ensure_ascii=False)
            analytics.answer_status = status
            analytics.delivery_status = delivery_status or (
                "delivered" if delivered else "failed"
            )
            analytics.sent_at = datetime.now(timezone.utc) if delivered else None
            if delivered:
                reply = Message(conversation_id=analytics.conversation_id, role="assistant", body=answer)
                session.add(reply)
                await session.flush()
                session.add(Answer(message_id=reply.id, question_id=question_id, text=answer, status=status))

    async def suppress_delivery(self, *, question_id: int) -> None:
        async with self.session() as session, session.begin():
            analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
            if analytics is None:
                raise ValueError("Question not found")
            analytics.answer_status = "unverified"
            analytics.delivery_status = "suppressed"

    async def save_resolution(self, *, question_id: int, telegram_id: int, value: str) -> None:
        if value not in {"resolved", "unresolved"}:
            raise ValueError("Invalid resolution")
        async with self.session() as session, session.begin():
            question = await session.get(Question, question_id)
            if question is None or question.telegram_id != telegram_id:
                raise ValueError("Question not found")
            analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
            if analytics is None:
                analytics = Analitics(question_id=question_id)
                session.add(analytics)
            analytics.resolution = value

    async def record_escalation(self, *, question_id: int, status: str) -> None:
        if status not in {"offered", "requested", "delivered", "failed"}:
            raise ValueError("Invalid escalation status")
        async with self.session() as session, session.begin():
            analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
            if analytics is None:
                raise ValueError("Question not found")
            session.add(EscalationEvent(question_id=question_id, status=status))
            if status != "offered" and analytics.operator_status != "delivered":
                analytics.operator_status = status

    async def clarification_count(self, *, telegram_id: int, chat_id: int) -> int:
        async with self.session() as session:
            statuses = (await session.scalars(
                select(Analitics.answer_status).join(Question, Question.id == Analitics.question_id)
                .where(Question.telegram_id == telegram_id, Question.chat_id == chat_id,
                       Analitics.answer_status.is_not(None))
                .order_by(Question.id.desc()).limit(4)
            )).all()
        count = 0
        for status in statuses:
            if status != "no_evidence":
                break
            count += 1
        return count

    async def save_question_labels(
        self,
        labels: list[tuple[int, str, str, str | None]],
    ) -> None:
        """Record labels without changing the question's escalation state."""
        async with self.session() as session, session.begin():
            for question_id, area, kind, issue in labels:
                values = {
                    "area": area,
                    "kind": kind,
                    "issue": issue,
                    "classified_at": func.current_timestamp(),
                }
                statement = self._insert(
                    QuestionAnalytics, {"question_id": question_id, **values}
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[QuestionAnalytics.question_id],
                        set_=values,
                        where=QuestionAnalytics.classified_at.is_(None),
                    )
                )

    async def latest_question_id(self, *, telegram_id: int, chat_id: int) -> int | None:
        """Find the latest processable question for a direct operator request."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        async with self.session() as session:
            return await session.scalar(
                select(Question.id)
                .join(Analitics, Analitics.question_id == Question.id)
                .where(
                    Question.telegram_id == telegram_id,
                    Question.chat_id == chat_id,
                    or_(
                        Analitics.received_at >= cutoff,
                        and_(
                            Analitics.received_at.is_(None),
                            Question.created_at >= cutoff.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                        ),
                    ),
                    or_(
                        Analitics.delivery_status.is_(None),
                        Analitics.delivery_status.not_in(
                            ("suppressed", "rate_limited")
                        ),
                    ),
                )
                .order_by(Question.id.desc())
                .limit(1)
            )

    async def get_question_context(
        self, *, question_id: int, telegram_id: int, max_messages: int = 8
    ) -> dict:
        """Return only the requester's question and a bounded conversation tail."""
        if not 1 <= max_messages <= 20:
            raise ValueError("max_messages must be from 1 to 20")
        async with self.session() as session:
            question = await session.get(Question, question_id)
            if question is None or question.telegram_id != telegram_id:
                raise ValueError("Question not found")
            analytics = await session.scalar(
                select(Analitics).where(Analitics.question_id == question_id)
            )
            messages: list[dict[str, str]] = []
            if analytics is not None and analytics.conversation_id is not None:
                answer_message_id = await session.scalar(
                    select(Answer.message_id).where(Answer.question_id == question_id)
                )
                if answer_message_id is not None:
                    stmt = select(Message.role, Message.body).where(
                        Message.conversation_id == analytics.conversation_id,
                        Message.role.in_(("user", "assistant")),
                        Message.id <= answer_message_id,
                    )
                    rows = (await session.execute(
                        stmt.order_by(Message.id.desc()).limit(max_messages)
                    )).all()
                    messages = [
                        {"role": role, "body": body[:2000]}
                        for role, body in reversed(rows)
                    ]
            try:
                sources = json.loads(question.sources)
            except (TypeError, ValueError):
                sources = []
            return {
                "question_id": question.id,
                "question": question.question,
                "answer": question.answer,
                "sources": sources if isinstance(sources, list) else [],
                "chat_id": question.chat_id,
                "messages": messages,
            }

    async def enqueue_ticket(
        self,
        *,
        kind: str,
        provider: str,
        dedupe_key: str,
        payload: dict,
        question_id: int | None = None,
        telegram_id: int | None = None,
        issue_area: str | None = None,
        issue_kind: str | None = None,
        issue_title: str | None = None,
    ) -> tuple[TicketJob, bool]:
        """Create one durable job per question or recurring issue."""
        if kind not in {"support", "improvement"}:
            raise ValueError("Invalid ticket kind")
        if not provider or len(provider) > 32 or not dedupe_key or len(dedupe_key) > 200:
            raise ValueError("Invalid provider or dedupe key")
        if not isinstance(payload, dict) or not all(
            isinstance(payload.get(key), str) and payload[key].strip()
            for key in ("title", "description")
        ):
            raise ValueError("Ticket payload requires a title and description")
        if kind == "support" and (question_id is None or telegram_id is None):
            raise ValueError("Support ticket requires question and requester")
        if kind == "improvement" and (
            question_id is not None or not all((issue_area, issue_kind, issue_title))
        ):
            raise ValueError("Improvement task requires an issue group")

        values = {
            "kind": kind,
            "provider": provider,
            "dedupe_key": dedupe_key,
            "payload": dict(payload),
            "question_id": question_id,
            "telegram_id": None,
            "issue_area": issue_area,
            "issue_kind": issue_kind,
            "issue_title": issue_title,
            "state": "queued",
            "attempts": 0,
        }
        async with self.session() as session, session.begin():
            if question_id is not None:
                question = await session.get(Question, question_id)
                if question is None or question.telegram_id != telegram_id:
                    raise ValueError("Question not found")
                values["telegram_id"] = question.telegram_id
            inserted = await session.execute(
                self._insert(TicketJob, values).on_conflict_do_nothing()
            )
            ticket = await session.scalar(
                select(TicketJob).where(TicketJob.dedupe_key == dedupe_key)
            )
            if ticket is None and question_id is not None:
                ticket = await session.scalar(
                    select(TicketJob).where(TicketJob.question_id == question_id)
                )
            if ticket is None and kind == "improvement":
                ticket = await session.scalar(
                    select(TicketJob).where(
                        TicketJob.kind == "improvement",
                        TicketJob.issue_area == issue_area,
                        TicketJob.issue_kind == issue_kind,
                        TicketJob.issue_title == issue_title,
                    )
                )
            if ticket is None:
                raise RuntimeError("Ticket job could not be queued")
            if ticket.kind != kind:
                raise ValueError("Ticket dedupe key belongs to another request")
            if kind == "support" and ticket.question_id != question_id:
                raise ValueError("Ticket dedupe key belongs to another question")
            if kind == "improvement" and (
                ticket.issue_area, ticket.issue_kind, ticket.issue_title
            ) != (issue_area, issue_kind, issue_title):
                raise ValueError("Ticket dedupe key belongs to another issue")
            if inserted.rowcount and kind == "support":
                analytics = await session.scalar(
                    select(Analitics).where(Analitics.question_id == question_id)
                )
                if analytics is not None and analytics.operator_status != "delivered":
                    analytics.operator_status = "requested"
                session.add(EscalationEvent(question_id=question_id, status="requested"))
            return ticket, bool(inserted.rowcount)

    async def get_ticket_by_dedupe_key(self, dedupe_key: str) -> TicketJob | None:
        async with self.session() as session:
            return await session.scalar(
                select(TicketJob).where(TicketJob.dedupe_key == dedupe_key)
            )

    async def get_ticket_for_question(self, question_id: int) -> TicketJob | None:
        async with self.session() as session:
            return await session.scalar(
                select(TicketJob).where(TicketJob.question_id == question_id)
            )

    async def list_unnotified_tickets(self, *, limit: int = 50) -> list[TicketJob]:
        now = datetime.now(timezone.utc)
        async with self.session() as session:
            return list((await session.scalars(
                select(TicketJob).where(
                    TicketJob.kind == "support",
                    TicketJob.state.in_(("created", "closed")),
                    TicketJob.external_id.is_not(None),
                    TicketJob.notice_sent_at.is_(None),
                    or_(TicketJob.notice_next_attempt_at.is_(None),
                        TicketJob.notice_next_attempt_at <= now),
                ).order_by(TicketJob.id).limit(limit)
            )).all())

    async def list_uncertain_tickets(self, *, provider: str, limit: int = 10) -> list[TicketJob]:
        now = datetime.now(timezone.utc)
        async with self.session() as session, session.begin():
            jobs = list((await session.scalars(
                select(TicketJob).where(
                    TicketJob.provider == provider,
                    TicketJob.state == "uncertain",
                    or_(TicketJob.next_attempt_at.is_(None),
                        TicketJob.next_attempt_at <= now),
                ).order_by(TicketJob.id).limit(limit)
            )).all())
            for job in jobs:
                job.next_attempt_at = now + timedelta(minutes=5)
            return jobs

    async def record_ticket_notice(self, *, ticket_id: int, error: str | None) -> None:
        now = datetime.now(timezone.utc)
        async with self.session() as session, session.begin():
            ticket = await session.get(TicketJob, ticket_id)
            if ticket is None:
                raise ValueError("Ticket job not found")
            ticket.notice_attempts += 1
            if error is None:
                ticket.notice_sent_at = now
                ticket.notice_next_attempt_at = None
                ticket.notice_error = None
            else:
                ticket.notice_next_attempt_at = now + timedelta(
                    seconds=min(30 * 2 ** min(ticket.notice_attempts, 6), 3600)
                )
                ticket.notice_error = error[:255]

    async def get_ticket_for_issue(
        self, *, area: str, kind: str, title: str
    ) -> TicketJob | None:
        async with self.session() as session:
            return await session.scalar(
                select(TicketJob).where(
                    TicketJob.kind == "improvement",
                    TicketJob.issue_area == area,
                    TicketJob.issue_kind == kind,
                    TicketJob.issue_title == title,
                )
            )

    async def claim_next_ticket(
        self, *, provider: str, now: datetime | None = None
    ) -> TicketJob | None:
        """Atomically claim a queued job; ambiguous old claims need review."""
        now = now or datetime.now(timezone.utc)
        due = or_(
            TicketJob.next_attempt_at.is_(None),
            TicketJob.next_attempt_at <= now,
        )
        async with self.session() as session, session.begin():
            candidate = await session.scalar(
                select(TicketJob.id)
                .where(
                    TicketJob.provider == provider,
                    TicketJob.state.in_(("queued", "retry")),
                    due,
                )
                .order_by(TicketJob.id)
                .limit(1)
            )
            if candidate is None:
                return None
            result = await session.execute(
                update(TicketJob)
                .where(
                    TicketJob.id == candidate,
                    TicketJob.state.in_(("queued", "retry")),
                    due,
                )
                .values(
                    state="creating",
                    attempts=TicketJob.attempts + 1,
                    next_attempt_at=None,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return None
            return await session.get(TicketJob, candidate)

    async def mark_ticket_created(
        self, *, ticket_id: int, external_id: str, url: str, status: str
    ) -> TicketJob:
        if not external_id or not url or not status:
            raise ValueError("Provider response is incomplete")
        async with self.session() as session, session.begin():
            ticket = await session.get(TicketJob, ticket_id)
            if ticket is None:
                raise ValueError("Ticket job not found")
            if ticket.state in {"created", "closed"}:
                if ticket.external_id != external_id:
                    raise ValueError("Ticket job already linked elsewhere")
                return ticket
            if ticket.state not in {"creating", "uncertain"}:
                raise ValueError("Ticket job was not claimed")
            ticket.external_id = external_id
            ticket.external_url = url
            ticket.external_status = status
            ticket.state = "created"
            ticket.last_error = None
            now = datetime.now(timezone.utc)
            ticket.updated_at = now
            ticket.next_attempt_at = now + timedelta(minutes=5)
            if ticket.kind == "support" and ticket.question_id is not None:
                analytics = await session.scalar(
                    select(Analitics).where(Analitics.question_id == ticket.question_id)
                )
                if analytics is not None:
                    analytics.operator_status = "delivered"
                session.add(EscalationEvent(
                    question_id=ticket.question_id, status="delivered"
                ))
            return ticket

    async def mark_ticket_failure(
        self, *, ticket_id: int, error: str, retryable: bool,
        uncertain: bool = False, now: datetime | None = None
    ) -> TicketJob:
        now = now or datetime.now(timezone.utc)
        async with self.session() as session, session.begin():
            ticket = await session.get(TicketJob, ticket_id)
            if ticket is None or ticket.state != "creating":
                raise ValueError("Ticket job was not claimed")
            ticket.state = "uncertain" if uncertain else "retry" if retryable else "failed"
            ticket.last_error = error[:1000]
            ticket.next_attempt_at = (
                now + timedelta(seconds=min(30 * 2 ** min(ticket.attempts, 6), 3600))
                if ticket.state == "retry" else
                now + timedelta(minutes=5) if ticket.state == "uncertain" else None
            )
            ticket.updated_at = now
            if ticket.kind == "support" and ticket.state == "failed":
                analytics = await session.scalar(
                    select(Analitics).where(Analitics.question_id == ticket.question_id)
                )
                if analytics is not None and analytics.operator_status != "delivered":
                    analytics.operator_status = "failed"
                session.add(EscalationEvent(
                    question_id=ticket.question_id, status="failed"
                ))
            return ticket

    async def update_ticket_status(
        self, *, ticket_id: int, status: str, closed: bool
    ) -> TicketJob:
        """Apply a provider status update to the linked ticket."""
        async with self.session() as session, session.begin():
            ticket = await session.get(TicketJob, ticket_id)
            if ticket is None or ticket.external_id is None:
                raise ValueError("Linked ticket not found")
            ticket.external_status = status[:64]
            ticket.state = "closed" if closed else "created"
            now = datetime.now(timezone.utc)
            ticket.closed_at = (ticket.closed_at or now) if closed else None
            ticket.next_attempt_at = None if closed else now + timedelta(minutes=5)
            ticket.updated_at = now
            return ticket

    async def list_pollable_tickets(
        self, *, provider: str, limit: int = 50,
        now: datetime | None = None
    ) -> list[TicketJob]:
        """Lease due status polls so an API failure still respects cadence."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be from 1 to 500")
        now = now or datetime.now(timezone.utc)
        async with self.session() as session, session.begin():
            jobs = list((await session.scalars(
                select(TicketJob).where(
                    TicketJob.provider == provider,
                    TicketJob.state == "created",
                    TicketJob.external_id.is_not(None),
                    or_(
                        TicketJob.next_attempt_at.is_(None),
                        TicketJob.next_attempt_at <= now,
                    ),
                ).order_by(TicketJob.id).limit(limit)
            )).all())
            if jobs:
                await session.execute(
                    update(TicketJob)
                    .where(TicketJob.id.in_([job.id for job in jobs]))
                    .values(
                        next_attempt_at=now + timedelta(minutes=5),
                        updated_at=now,
                    )
                )
            return jobs

    async def mark_stale_claims_uncertain(self, *, before: datetime) -> int:
        """After a crash, do not retry a create call with an unknown outcome."""
        async with self.session() as session, session.begin():
            result = await session.execute(
                update(TicketJob)
                .where(
                    TicketJob.state == "creating",
                    TicketJob.updated_at < before,
                )
                .values(state="uncertain", last_error="Creation result unknown")
            )
            return result.rowcount

    async def save_feedback(
        self,
        *,
        question_id: int,
        telegram_id: int,
        value: str,
    ) -> None:
        if value not in ("helpful", "not_helpful"):
            raise ValueError("Некорректная оценка ответа")
        statement = self._insert(
            Feedback, {"question_id": question_id, "telegram_id": telegram_id, "value": value}
        )
        statement = statement.on_conflict_do_update(
            index_elements=[Feedback.question_id, Feedback.telegram_id],
            set_={"value": value, "created_at": func.current_timestamp()},
        )
        async with self.session() as session:
            question = await session.get(Question, question_id)
            if question is None or question.telegram_id != telegram_id:
                raise ValueError("Вопрос не найден")
            await session.execute(statement)
            await session.commit()
