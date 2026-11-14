"""Prepare PDF chunks for the bot; runs separately from Telegram polling."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.ingest.light import parse_document as parse_light
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


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF; по умолчанию все из KNOWLEDGE_DIR")
    parser.add_argument("--parser", choices=("auto", "docling", "light"), default="auto")
    parser.add_argument("--force", action="store_true", help="обработать заново даже при готовом индексе")
    args = parser.parse_args()

    documents = [args.pdf] if args.pdf else sorted(settings.knowledge_dir.glob("*.pdf"))
    if not documents:
        parser.error(f"Нет PDF в {settings.knowledge_dir}")
    for pdf in documents:
        pdf = pdf.resolve()
        if not pdf.is_file():
            parser.error(f"Файл не найден: {pdf}")
        if not args.force and is_current(pdf):
            print(f"Уже обработан: {pdf.name}")
            continue

        backend = args.parser
        chunks = parse_document(pdf, out_dir=pdf.parent)

        print(f"Готово: {len(chunks)} чанков для {pdf.name}")


if __name__ == "__main__":
    main()
