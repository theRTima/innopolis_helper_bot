"""Evaluate BM25 retrieval against hand-labeled document and page references."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.rag.index import SearchIndex, load_chunks_from_database
from app.storage.db import Database


def load_cases(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def evaluate(dataset: Path, top_k: int) -> int:
    cases = load_cases(dataset)
    settings = Settings.from_env()
    database = Database(settings.database_url or settings.database_path)
    await database.init()
    try:
        chunks = await load_chunks_from_database(database)
    finally:
        await database.close()

    if not chunks:
        print("В БД нет активных чанков. Сначала импортируй документы.", file=sys.stderr)
        return 1

    index = SearchIndex(chunks)
    reciprocal_ranks: list[float] = []
    hits = 0
    for case in cases:
        results = index.search(case["query"], top_k=top_k)
        expected_document = case["expected_document"]
        expected_pages = set(case["expected_pages"])
        rank = next(
            (
                position
                for position, (_, chunk) in enumerate(results, start=1)
                if chunk.document == expected_document
                and any(page in expected_pages for page in range(chunk.page_from, chunk.page_to + 1))
            ),
            None,
        )
        if rank is None:
            reciprocal_ranks.append(0.0)
            print(f"MISS {case['id']}: {case['query']}")
        else:
            hits += 1
            reciprocal_ranks.append(1 / rank)
            print(f"HIT  {case['id']}: rank={rank}, {expected_document}, стр. {results[rank - 1][1].page_label}")

    total = len(cases)
    mrr = sum(reciprocal_ranks) / total if total else 0.0
    hit_rate = hits / total if total else 0.0
    print(f"\nHitRate@{top_k}: {hits}/{total} ({hit_rate:.1%})")
    print(f"MRR@{top_k}: {mrr:.3f}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "data/eval/retrieval_v1.jsonl",
        help="JSONL с запросами и ожидаемыми документами/страницами",
    )
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(evaluate(args.dataset, args.top_k)))


if __name__ == "__main__":
    main()
