"""Prepare PDF chunks for the bot; runs separately from Telegram polling."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from time import perf_counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.ingest.service import import_document
from app.ingest.light import parse_document as parse_light
from app.storage.db import Database
from app.ingestion import vision_parser as vision
from app.storage.models import ImportRun


def is_current(pdf: Path) -> bool:
    meta_path = pdf.with_name(f"{pdf.stem}.chunks.meta.json")
    chunks_path = pdf.with_name(f"{pdf.stem}.chunks.jsonl")
    if not meta_path.is_file() or not chunks_path.is_file():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return (
            meta["complete"]
            and meta["source_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
            and meta["chunks_sha256"] == hashlib.sha256(chunks_path.read_bytes()).hexdigest()
        )
    except (KeyError, TypeError, ValueError):
        return False


def load_cached_chunks(pdf: Path) -> list[dict]:
    chunks_path = pdf.with_name(f"{pdf.stem}.chunks.jsonl")
    return [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def import_pdfs(settings: Settings, documents: list[Path], args: argparse.Namespace) -> None:
    database = Database(settings.database_url or settings.database_path)
    await database.init()
    try:
        for pdf in documents:
            total_started = perf_counter()
            before = vision.metrics_snapshot()
            stats: dict[str, int] = {}
            pdf = pdf.resolve()
            if not pdf.is_file():
                raise FileNotFoundError(pdf)

            if not args.force and args.parser == "auto" and is_current(pdf):
                chunks = load_cached_chunks(pdf)
                print(f"Использую готовый результат парсинга: {pdf.name}")
            else:
                if args.parser == "light":
                    chunks = parse_light(pdf, out_dir=pdf.parent, text_only=args.text_only, stats=stats)
                else:
                    from app.ingestion.docling_parser import parse_document
                    chunks = parse_document(pdf, out_dir=pdf.parent)

            parse_ms = round((perf_counter() - total_started) * 1000)
            after = vision.metrics_snapshot()
            async with database.session() as session:
                async with session.begin():
                    version, imported = await import_document(session, pdf, chunks)
                    session.add(ImportRun(
                        document_id=version.document_id,
                        document_version_id=version.id,
                        parser=args.parser,
                        imported=imported,
                        chunk_count=len(chunks),
                        model_calls=after["model_calls"] - before["model_calls"],
                        cache_hits=after["cache_hits"] - before["cache_hits"],
                        parse_ms=parse_ms,
                        text_ms=stats.get("text_ms", 0),
                        vision_ms=after["vision_ms"] - before["vision_ms"],
                        total_ms=round((perf_counter() - total_started) * 1000),
                    ))

            result = "импортирована" if imported else "без изменений"
            print(
                f"{pdf.name}: {result}; версия {version.version}, "
                f"чанков {len(chunks)}; парсинг {parse_ms} мс; "
                f"модель {after['model_calls'] - before['model_calls']} запросов, "
                f"кэш {after['cache_hits'] - before['cache_hits']} попаданий"
            )
    finally:
        await database.close()


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF; по умолчанию все из KNOWLEDGE_DIR")
    parser.add_argument("--parser", choices=("auto", "docling", "light"), default="auto")
    parser.add_argument("--force", action="store_true", help="повторно обработать документы")
    parser.add_argument("--text-only", action="store_true", help="пропустить картинки в light parser")
    args = parser.parse_args()
    if args.text_only and args.parser != "light":
        parser.error("--text-only requires --parser light")

    documents = [args.pdf] if args.pdf else sorted(settings.knowledge_dir.glob("*.pdf"))
    if not documents:
        parser.error(f"Нет PDF в {settings.knowledge_dir}")
    asyncio.run(import_pdfs(settings, documents, args))


if __name__ == "__main__":
    main()
