"""Prepare PDF chunks for the bot; runs separately from Telegram polling."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.ingest.service import import_document
from app.ingest.light import parse_document as parse_light
from app.storage.db import Database
from docling_parser import parse_document


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
            pdf = pdf.resolve()
            if not pdf.is_file():
                raise FileNotFoundError(pdf)

            if not args.force and is_current(pdf):
                chunks = load_cached_chunks(pdf)
                print(f"Использую готовый результат парсинга: {pdf.name}")
            else:
                parser = parse_light if args.parser == "light" else parse_document
                chunks = parser(pdf, out_dir=pdf.parent)

            async with database.session() as session:
                async with session.begin():
                    version, imported = await import_document(session, pdf, chunks)

            result = "импортирована" if imported else "без изменений"
            print(
                f"{pdf.name}: {result}; версия {version.version}, "
                f"чанков {len(chunks)}"
            )
    finally:
        await database.close()


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF; по умолчанию все из KNOWLEDGE_DIR")
    parser.add_argument("--parser", choices=("auto", "docling", "light"), default="auto")
    parser.add_argument("--force", action="store_true", help="повторно обработать документы")
    args = parser.parse_args()

    documents = [args.pdf] if args.pdf else sorted(settings.knowledge_dir.glob("*.pdf"))
    if not documents:
        parser.error(f"Нет PDF в {settings.knowledge_dir}")
    asyncio.run(import_pdfs(settings, documents, args))


if __name__ == "__main__":
    main()
