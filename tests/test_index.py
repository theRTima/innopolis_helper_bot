import json

from app.rag.index import Chunk, SearchIndex, load_chunks, tokenize


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


def test_prepared_chunks_are_loaded_instead_of_reparsing_pdf(tmp_path) -> None:
    (tmp_path / "Документ.pdf").write_bytes(b"not a valid PDF")
    (tmp_path / "Документ.chunks.jsonl").write_text(
        json.dumps(
            {"id": "Документ.pdf:p3:c0", "page": 3, "text": "Схема согласования заявки"},
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    chunks = load_chunks(tmp_path)

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "Документ.pdf:p3:c0"
    assert chunks[0].document == "Документ"
    assert chunks[0].page_from == 3
