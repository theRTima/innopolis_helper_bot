"""Async SQLAlchemy storage with Alembic migrations."""

from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.storage.models import Analitics, Answer, Conversation, Feedback, Message, Question, User


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

    async def save_question(
        self,
        *,
        telegram_id: int,
        chat_id: int,
        question: str,
        answer: str | None = None,
        sources: list[dict[str, object]] | None = None,
        status: str | None = None,
    ) -> int:
        row = Question(
            telegram_id=telegram_id,
            chat_id=chat_id,
            question=question,
            answer="",
            sources="[]",
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
                session.add(Analitics(question_id=row.id))

            question_id = row.id
        if answer is not None:
            await self.save_answer(
                question_id=question_id,
                answer=answer,
                sources=sources or [],
                status=status or ("with_sources" if sources else "unverified"),
            )
        return question_id

    async def save_answer(self, *, question_id: int, answer: str,
                          sources: list[dict[str, object]], status: str) -> None:
        if status not in {"with_sources", "no_evidence", "unverified", "error"}:
            raise ValueError("Некорректный статус ответа")
        async with self.session() as session:
            async with session.begin():
                question = await session.get(Question, question_id)
                if question is None:
                    raise ValueError("Вопрос не найден")
                question.answer = answer
                question.sources = json.dumps(sources, ensure_ascii=False)
                analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
                if analytics is None:
                    analytics = Analitics(question_id=question_id)
                    session.add(analytics)
                analytics.answer_status = status
                existing = await session.scalar(select(Answer).where(Answer.question_id == question_id))
                if existing is not None:
                    existing.text = answer
                    existing.status = status
                else:
                    conversation_id = await session.scalar(
                        select(Conversation.id).join(User, Conversation.user_id == User.id).where(
                            User.telegram_id == question.telegram_id,
                            Conversation.chat_id == question.chat_id,
                        )
                    )
                    reply = Message(conversation_id=conversation_id, role="assistant", body=answer)
                    session.add(reply)
                    await session.flush()
                    session.add(Answer(message_id=reply.id, question_id=question_id, text=answer, status=status))

    async def save_operator_status(self, *, question_id: int, telegram_id: int, status: str) -> None:
        if status not in {"requested", "delivered", "failed"}:
            raise ValueError("Некорректный статус оператора")
        async with self.session() as session:
            async with session.begin():
                question = await session.get(Question, question_id)
                if question is None or question.telegram_id != telegram_id:
                    raise ValueError("Вопрос не найден")
                analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
                if analytics is None:
                    analytics = Analitics(question_id=question_id)
                    session.add(analytics)
                analytics.operator_status = status

    async def save_feedback(
        self,
        *,
        question_id: int,
        telegram_id: int,
        value: str,
    ) -> int:
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
            async with session.begin():
                await session.execute(statement)
                count = await session.scalar(
                    select(func.count()).select_from(Feedback).where(
                        Feedback.value == "not_helpful", Feedback.telegram_id == telegram_id
                    )
                )
            return int(count or 0)
