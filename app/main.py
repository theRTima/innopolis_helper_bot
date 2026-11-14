"""Bot entrypoint: wire configuration, retrieval, LLM and storage together."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramUnauthorizedError

from app.bot.handlers import router
from app.config import Settings
from app.llm.client import LLMClient
from app.rag.index import (
    SearchIndex,
    chunk_source_mismatches,
    load_chunks_auto,
    load_chunks_from_database,
)
from app.storage.db import Database

logger = logging.getLogger(__name__)


async def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    try:
        settings.validate()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    database = Database(settings.database_url or settings.database_path)
    await database.init()

    logger.info("Загружаю активные версии чанков из БД и файловых индексов")
    database_chunks = await load_chunks_from_database(database)
    file_chunks, file_mode = load_chunks_auto(
        settings.chunks_dir, settings.documents_dir
    )
    database_documents = {chunk.document for chunk in database_chunks}
    file_additions = [
        chunk for chunk in file_chunks if chunk.document not in database_documents
    ]
    chunks = database_chunks + file_additions
    if database_chunks and file_additions:
        source_mode = f"database+{file_mode}"
    elif database_chunks:
        source_mode = "database"
    else:
        source_mode = file_mode
    if not chunks:
        logger.error(
            "Чанки не найдены. Выполните python -m app.ingestion "
            "или полный rag_pipeline: ingest -> render -> extract --all."
        )
        await database.close()
        return 1

    if source_mode == "flat":
        missing, stale = chunk_source_mismatches(
            chunks, settings.knowledge_dir
        )
        if missing or stale:
            details = []
            recovery = []
            if missing:
                details.append(f"не обработаны PDF: {', '.join(missing)}")
                recovery.append("сначала выполните: python -m app.ingestion")
            if stale:
                details.append(
                    f"нет исходных PDF для чанков: {', '.join(stale)}"
                )
                recovery.append(
                    f"удалите устаревшие *.chunks.jsonl из {settings.chunks_dir}"
                )
            logger.error("%s. %s", "; ".join(details), "; ".join(recovery))
            await database.close()
            return 1
    else:
        pdf_sources = {
            path.stem for path in settings.knowledge_dir.glob("*.pdf")
        }
        chunk_sources = {chunk.document for chunk in chunks}
        missing = sorted(pdf_sources - chunk_sources)
        if missing:
            logger.error(
                "Не обработаны PDF: %s. Выполните app.ingestion или rag_pipeline "
                "ingest -> render -> extract --all.",
                ", ".join(missing),
            )
            await database.close()
            return 1
    logger.info("Загружено %d фрагментов (%s)", len(chunks), source_mode)
    index = SearchIndex(chunks)

    llm = LLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        session_id=settings.llm_session_id,
        user_agent=settings.llm_user_agent,
        max_tokens=settings.answer_max_tokens,
        temperature=settings.temperature,
    )

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(link_preview_is_disabled=True),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        logger.info("Бот запущен")
        await dispatcher.start_polling(
            bot,
            index=index,
            llm=llm,
            db=database,
            settings=settings,
        )
    except TelegramUnauthorizedError:
        print("Telegram отклонил токен. Проверьте TELEGRAM_BOT_TOKEN.", file=sys.stderr)
        return 1
    finally:
        await database.close()
        await bot.session.close()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
