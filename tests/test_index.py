import json

import pytest

from app.rag.index import (
    Chunk,
    SearchIndex,
    chunk_source_mismatches,
    load_chunks,
    tokenize,
)


def test_tokenize_keeps_russian_words_and_numbers() -> None:
    assert tokenize("Статус мебели 2334-26!") == ["статус", "мебели", "2334", "26"]


def test_search_returns_relevant_chunk() -> None:
    chunks = [
        Chunk(
            chunk_id="furniture-1",
            document="Реестр мебели",
            page_from=6,
            page_to=6,
            text="Статус мебели: занят, свободен, списан.",
        ),
        Chunk(
            chunk_id="trip-1",
            document="БП Командировка",
            page_from=3,
            page_to=3,
            text="Порядок оформления командировки и отчётность.",
        ),
    ]
    index = SearchIndex(chunks)

    results = index.search("какие статусы мебели", top_k=2)

    assert results
    assert results[0][1].chunk_id == "furniture-1"


def test_search_without_terms_returns_nothing() -> None:
    index = SearchIndex([])
    assert index.search("") == []


def test_load_chunks_reads_docling_jsonl(tmp_path) -> None:
    chunks_path = tmp_path / "travel.chunks.jsonl"
    chunks_path.write_text(
        json.dumps(
            {
                "id": "БП Командировка.pdf:p4:c0",
                "source": "БП Командировка.pdf",
                "page": 4,
                "text": "Оформление командировки",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    assert load_chunks(tmp_path) == [
        Chunk(
            chunk_id="БП Командировка.pdf:p4:c0",
            document="БП Командировка",
            page_from=4,
            page_to=4,
            text="Оформление командировки",
        )
    ]


def test_load_chunks_reports_malformed_json_with_location(tmp_path) -> None:
    chunks_path = tmp_path / "bad.chunks.jsonl"
    chunks_path.write_text("{bad json}\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"bad\.chunks\.jsonl:1: invalid JSON"):
        load_chunks(tmp_path)


def test_chunk_source_mismatches_finds_missing_and_stale_sources(tmp_path) -> None:
    (tmp_path / "current.pdf").touch()
    chunks = [
        Chunk("current:1", "current", 1, 1, "current"),
        Chunk("removed:1", "removed", 1, 1, "removed"),
    ]

    assert chunk_source_mismatches(chunks, tmp_path) == ([], ["removed"])


def test_toc_chunks_are_skipped_for_normal_questions() -> None:
    chunks = [
        Chunk(
            chunk_id="toc",
            document="Документ",
            page_from=2,
            page_to=2,
            text="Схема процесса командировки ......... 4",
            title="Table of Contents",
        ),
        Chunk(
            chunk_id="body",
            document="Документ",
            page_from=7,
            page_to=7,
            text="Схема процесса командировки описана в инструкции.",
            title="5 Инструкция",
        ),
    ]
    index = SearchIndex(chunks)

    results = index.search("схема процесса командировки", top_k=1)

    assert [chunk.chunk_id for _score, chunk in results] == ["body"]


def test_visual_chunks_are_added_for_visual_queries() -> None:
    chunks = [
        Chunk(
            chunk_id="text",
            document="Документ",
            page_from=2,
            page_to=2,
            text="Схема схемы процесса процесса командировки командировки.",
        ),
        Chunk(
            chunk_id="diagram",
            document="Документ",
            page_from=4,
            page_to=4,
            text="Инициация БП. Согласование. Заказ билетов нужен?",
            title="2 Схема бизнес-процесса",
            chunk_type="diagram",
        ),
    ]
    index = SearchIndex(chunks)

    results = index.search("расскажи про схему командировки", top_k=1)

    assert any(chunk.chunk_id == "diagram" for _score, chunk in results)
