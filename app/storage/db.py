"""SQLite storage for questions and helpful/not helpful feedback."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    sources TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    telegram_id INTEGER NOT NULL,
    value TEXT NOT NULL CHECK (value IN ('helpful', 'not_helpful')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (question_id, telegram_id)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self._path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.executescript(SCHEMA)
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("База данных не инициализирована")
        return self._connection

    async def save_question(
        self,
        *,
        telegram_id: int,
        chat_id: int,
        question: str,
        answer: str,
        sources: list[dict[str, object]],
    ) -> int:
        cursor = await self.connection.execute(
            "INSERT INTO questions (telegram_id, chat_id, question, answer, sources) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                telegram_id,
                chat_id,
                question,
                answer,
                json.dumps(sources, ensure_ascii=False),
            ),
        )
        await self.connection.commit()
        return int(cursor.lastrowid)

    async def save_feedback(
        self,
        *,
        question_id: int,
        telegram_id: int,
        value: str,
    ) -> None:
        await self.connection.execute(
            "INSERT INTO feedback (question_id, telegram_id, value) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT (question_id, telegram_id) "
            "DO UPDATE SET value = excluded.value, created_at = CURRENT_TIMESTAMP",
            (question_id, telegram_id, value),
        )
        await self.connection.commit()
