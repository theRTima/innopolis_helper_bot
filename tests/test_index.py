from app.rag.index import (
    Chunk,
    SearchIndex,
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
