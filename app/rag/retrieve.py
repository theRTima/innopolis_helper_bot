"""Bounded context expansion over existing chunks; no model calls."""

import re

from app.rag.context import format_context
from app.rag.representation import is_navigation


def retrieve_context(
    index, query, *, top_k=2, min_score=0.0, max_chunks=6, max_chars=12000
):
    """Keep seeds, then add high-ranked or structurally related evidence.

    Budget includes source labels and separators. Whole chunks only: never
    truncate a condition, table, or sentence to fit the remaining budget.
    """
    if top_k < 1 or max_chunks < 1 or max_chars < 1:
        raise ValueError("Retrieval limits must be positive")
    ranked = index.search(query, top_k=max(top_k, 12), min_score=min_score)
    if not ranked:
        return []
    # Precise schedule/contact/number lookups do not need a process overview.
    focused = bool(
        re.search(r"график|телефон|расписани|во сколько", query, re.I)
    )
    seed_count = min(top_k, 2) if focused else top_k
    seeds = ranked[:seed_count]
    limit = min(max_chunks, seed_count) if focused else max_chunks
    selected = []
    seen = set()

    def append(result):
        chunk = result[1]
        identity = (
            chunk.document_id or chunk.document,
            chunk.version_id,
            chunk.chunk_id,
        )
        if identity in seen or len(selected) >= limit:
            return
        if len(format_context(selected + [result])) > max_chars:
            return
        selected.append(result)
        seen.add(identity)

    for result in seeds:
        append(result)
    if not selected:
        return []
    scope = (
        seeds[0][1].document_id or seeds[0][1].document,
        seeds[0][1].version_id,
        seeds[0][1].chunk_file,
    )
    for rank, result in enumerate(ranked[seed_count:], seed_count + 1):
        score, chunk = result
        if (
            chunk.document_id or chunk.document,
            chunk.version_id,
            chunk.chunk_file,
        ) != scope:
            continue
        if score < ranked[0][0] * 0.25 or is_navigation(chunk):
            continue
        if chunk.chunk_type in {"image", "mixed"} and not re.search(
            r"картин|изображ|значок|иконк|выгляд|скриншот", query, re.I
        ):
            continue
        related = rank <= max(seed_count, 4) or any(
            abs(chunk.page_from - seed.page_from) <= 1
            or (chunk.title == seed.title and chunk.title not in {"", "Важно"})
            for _, seed in seeds
        )
        if related:
            append(result)
    return selected
