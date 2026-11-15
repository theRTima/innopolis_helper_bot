"""Merge a backed-up SQLite bot database into an existing PostgreSQL database.

The destination must already be migrated and contain no documents. Run only
while both bot pollers are stopped; all inserts use one transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import JSON, DateTime, func, insert, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.storage.db import Database
from app.storage.models import (
    Analitics, Answer, Chunk, Citation, Conversation, Document,
    DocumentVersion, Feedback, ImportRun, Message, Question, User,
)


def source_rows(source: sqlite3.Connection, table: str) -> list[dict]:
    return [dict(row) for row in source.execute(f"SELECT * FROM {table} ORDER BY id")]


def converted(model, row: dict, **overrides) -> dict:
    values = {}
    for column in model.__table__.columns:
        if column.name == "id" or column.name not in row:
            continue
        value = row[column.name]
        if value is not None and isinstance(column.type, DateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value is not None and isinstance(column.type, JSON) and isinstance(value, str):
            value = json.loads(value)
        values[column.name] = value
    values.update(overrides)
    return values


async def merge(source_path: Path, database_url: str, expected_questions: int) -> None:
    source = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite backup failed integrity_check")
    database = Database(database_url)
    await database.init()
    maps: dict[str, dict[int, int]] = {name: {} for name in (
        "users", "conversations", "documents", "document_versions", "chunks",
        "questions", "messages", "answers", "feedback", "citations", "analytics", "import_runs",
    )}
    try:
        async with database.session() as session:
            async with session.begin():
                questions = await session.scalar(select(func.count()).select_from(Question))
                documents = await session.scalar(select(func.count()).select_from(Document))
                if questions != expected_questions or documents != 0:
                    raise ValueError(
                        f"Destination changed: questions={questions}, documents={documents}; "
                        f"expected {expected_questions} questions and 0 documents"
                    )

                async def add(table: str, model, row: dict, **overrides) -> int:
                    new_id = await session.scalar(
                        insert(model).values(**converted(model, row, **overrides)).returning(model.id)
                    )
                    maps[table][row["id"]] = new_id
                    return new_id

                for row in source_rows(source, "users"):
                    existing = await session.scalar(select(User.id).where(User.telegram_id == row["telegram_id"]))
                    maps["users"][row["id"]] = existing or await add("users", User, row)
                for row in source_rows(source, "conversations"):
                    user_id = maps["users"][row["user_id"]]
                    existing = await session.scalar(select(Conversation.id).where(
                        Conversation.user_id == user_id, Conversation.chat_id == row["chat_id"]
                    ))
                    maps["conversations"][row["id"]] = existing or await add(
                        "conversations", Conversation, row, user_id=user_id
                    )
                for row in source_rows(source, "documents"):
                    await add("documents", Document, row)
                for row in source_rows(source, "document_versions"):
                    await add("document_versions", DocumentVersion, row,
                              document_id=maps["documents"][row["document_id"]])
                for row in source_rows(source, "chunks"):
                    await add("chunks", Chunk, row,
                              document_version_id=maps["document_versions"][row["document_version_id"]])
                for row in source_rows(source, "questions"):
                    await add("questions", Question, row)
                for row in source_rows(source, "messages"):
                    await add("messages", Message, row,
                              conversation_id=maps["conversations"][row["conversation_id"]])
                for row in source_rows(source, "answers"):
                    await add("answers", Answer, row,
                              message_id=maps["messages"][row["message_id"]],
                              question_id=maps["questions"].get(row["question_id"]) if row["question_id"] else None)
                for row in source_rows(source, "feedback"):
                    await add("feedback", Feedback, row,
                              question_id=maps["questions"][row["question_id"]])
                for row in source_rows(source, "citations"):
                    await add("citations", Citation, row,
                              answer_id=maps["answers"][row["answer_id"]],
                              chunk_id=maps["chunks"][row["chunk_id"]])
                for row in source_rows(source, "analytics"):
                    await add("analytics", Analitics, row,
                              question_id=maps["questions"][row["question_id"]])
                for row in source_rows(source, "import_runs"):
                    await add("import_runs", ImportRun, row,
                              document_id=maps["documents"][row["document_id"]],
                              document_version_id=maps["document_versions"][row["document_version_id"]])
        print("Merged SQLite rows:", {table: len(mapping) for table, mapping in maps.items()})
    finally:
        source.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite_backup", type=Path)
    parser.add_argument("--expected-destination-questions", type=int, required=True)
    args = parser.parse_args()
    from os import environ
    database_url = environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+asyncpg://"):
        parser.error("DATABASE_URL must point to PostgreSQL using asyncpg")
    asyncio.run(merge(args.sqlite_backup, database_url, args.expected_destination_questions))


if __name__ == "__main__":
    main()
