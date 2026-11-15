"""Label stored questions once, or poll for new ones in a separate process."""

from __future__ import annotations

import argparse
import asyncio
import logging

from app.analytics.labeling import classify_pending
from app.analytics.taxonomy import load_areas
from app.config import Settings
from app.llm.client import LLMClient
from app.storage.db import Database

logger = logging.getLogger(__name__)


async def run(limit: int, interval: int) -> int:
    settings = Settings.from_env()
    areas = load_areas()
    database = Database(settings.database_url or settings.database_path)
    await database.init()
    model = LLMClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        session_id=settings.llm_session_id,
        user_agent=settings.llm_user_agent,
        max_tokens=settings.answer_max_tokens,
        temperature=settings.temperature,
    )
    try:
        while True:
            try:
                count, total = await classify_pending(
                    database, model, areas, limit=limit
                )
            except Exception as error:
                logger.error("Ошибка разметки (%s).", type(error).__name__)
                if not interval:
                    return 1
            else:
                if total or not interval:
                    logger.info("Размечено вопросов: %d из %d.", count, total)
                if not interval:
                    return 0 if count == total else 1
            await asyncio.sleep(interval)
    finally:
        await model.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Классификация сохранённых обращений для аналитики"
    )
    parser.add_argument(
        "--limit", type=int, default=60, help="Вопросов за проход"
    )
    parser.add_argument(
        "--watch",
        type=int,
        nargs="?",
        const=30,
        default=0,
        metavar="SECONDS",
        help="Повторять обработку, по умолчанию раз в 30 секунд",
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000 or args.watch < 0:
        parser.error("Укажите --limit от 1 до 1000 и --watch от 0")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        raise SystemExit(asyncio.run(run(args.limit, args.watch)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
