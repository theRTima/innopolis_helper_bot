"""Async SQLAlchemy storage with Alembic migrations."""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
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

    async def init(self) -> None:
        if self._engine is not None:
            return
        engine = create_async_engine(self.url)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
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
                session.add(Analitics(question_id=row.id, conversation_id=conversation_id,
                                      received_at=datetime.now(timezone.utc)))
                if answer is not None:
                    reply = Message(conversation_id=conversation_id, role="assistant", body=answer)
                    session.add(reply)
                    await session.flush()
                    session.add(Answer(message_id=reply.id, question_id=row.id, text=answer, status="generated"))
            return row.id

    async def save_delivery(self, *, question_id: int, answer: str, sources: list,
                            status: str, delivered: bool) -> None:
        if status not in {"with_sources", "no_evidence", "unverified", "error"}:
            raise ValueError("Invalid answer status")
        async with self.session() as session, session.begin():
            question = await session.get(Question, question_id)
            analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
            if question is None or analytics is None:
                raise ValueError("Question not found")
            question.answer = answer
            question.sources = json.dumps(sources, ensure_ascii=False)
            analytics.answer_status = status
            analytics.delivery_status = "delivered" if delivered else "failed"
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
        if status not in {"requested", "delivered", "failed"}:
            raise ValueError("Invalid escalation status")
        async with self.session() as session, session.begin():
            analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
            if analytics is None:
                raise ValueError("Question not found")
            session.add(EscalationEvent(question_id=question_id, status=status))
            if analytics.operator_status != "delivered":
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
