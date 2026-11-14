"""Parse every PDF in the knowledge directory into retrieval-ready JSONL."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.config import Settings
from app.ingestion.docling_parser import parse_document
from app.ingest.service import import_document
from app.storage.db import Database


async def parse_and_import(settings: Settings, args: argparse.Namespace, pdfs: list[Path]) -> None:
    database = Database(settings.database_url or settings.database_path)
    await database.init()
    try:
        for pdf_path in pdfs:
            print(f"Parsing {pdf_path.name}")
            chunks = parse_document(pdf_path, out_dir=args.out_dir, pages_spec=args.pages)
            if args.pages is not None:
                print("Partial page selection was written to files; database was not changed.")
                continue

            async with database.session() as session:
                async with session.begin():
                    version, imported = await import_document(session, pdf_path, chunks)
            result = "imported" if imported else "unchanged"
            print(f"Database: {result}; version {version.version}, chunks {len(chunks)}")
    finally:
        await database.close()


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pdf",
        nargs="?",
        type=Path,
        help="Parse only this PDF instead of every PDF in KNOWLEDGE_DIR",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=settings.knowledge_dir,
        help="PDF source directory (default: KNOWLEDGE_DIR)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=settings.chunks_dir,
        help="Chunk output directory (default: CHUNKS_DIR)",
    )
    parser.add_argument(
        "--pages",
        help="Optional 1-based page selection applied to each PDF, e.g. '4,7-9'",
    )
    args = parser.parse_args()

    if args.pdf is not None:
        pdf_path = args.pdf.expanduser().resolve()
        if not pdf_path.is_file():
            parser.error(f"PDF file does not exist: {pdf_path}")
        pdfs = [pdf_path]
    else:
        pdfs = sorted(args.knowledge_dir.expanduser().resolve().glob("*.pdf"))
    if not pdfs:
        parser.error(f"No PDF files found in {args.knowledge_dir}")

    asyncio.run(parse_and_import(settings, args, pdfs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
