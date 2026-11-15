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
from app.rag.index import SearchIndex, load_versioned_chunks
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

    logger.info(
        "Загружаю фрагменты из versioned store: %s",
        settings.documents_dir,
    )
    chunks = load_versioned_chunks(settings.documents_dir)
    if not chunks:
        logger.error(
            "Чанки не найдены. Выполните: "
            "app.rag.ingest -> app.rag.render -> app.rag.extract --all."
        )
        return 1

    pdf_sources = {
        path.stem for path in settings.knowledge_dir.glob("*.pdf")
    }
    chunk_sources = {chunk.document for chunk in chunks}
    missing = sorted(pdf_sources - chunk_sources)
    if missing:
        logger.warning(
            "Не обработаны PDF и будут недоступны: %s. "
            "Выполните ingest -> render -> extract.",
            ", ".join(missing),
        )
    logger.info("Загружено %d фрагментов (versioned)", len(chunks))
    index = SearchIndex(chunks)

    database = Database(settings.database_url or settings.database_path)
    await database.init()

    llm = LLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        session_id=settings.llm_session_id,
        user_agent=settings.llm_user_agent,
        reasoning_effort=settings.llm_answer_reasoning_effort,
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
