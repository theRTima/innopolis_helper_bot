import pytest

from app.rag.context import format_context
from app.rag.index import Chunk
from app.rag.retrieve import retrieve_context


def c(name, page, *, doc="doc", version="v1", text="Some evidence"):
    return Chunk(
        name,
        doc,
        page,
        page,
        text,
        document_id=doc,
        version_id=version,
        title="Instruction",
    )


class Index:
    def __init__(self, results):
        self.results = results

    def search(self, *args, **kwargs):
        return self.results[: kwargs["top_k"]]


def test_expansion_keeps_seeds_and_recovers_related_instructions():
    results = [(10, c("a", 7)), (9, c("b", 11)), (8, c("c", 9))]
    assert retrieve_context(Index(results), "Опиши шаги") == results


def test_no_expansion_to_another_document_or_version():
    results = [
        (10, c("a", 7)),
        (9, c("b", 8)),
        (8, c("c", 9, version="old")),
        (7, c("d", 9, doc="other")),
    ]
    assert retrieve_context(Index(results), "Опиши шаги") == results[:2]


def test_budget_is_exact_and_chunks_are_not_truncated():
    results = [(10, c("a", 7)), (9, c("b", 8))]
    budget = len(format_context(results[:1]))
    actual = retrieve_context(Index(results), "Опиши шаги", max_chars=budget)
    assert actual == results[:1]
    assert len(format_context(actual)) <= budget


def test_focused_question_does_not_expand_and_duplicates_removed():
    results = [(10, c("a", 3)), (9, c("b", 8)), (8, c("c", 9))]
    assert retrieve_context(Index(results), "График работы?") == results[:2]
    duplicate = Index([results[0], results[0], results[2]])
    assert len(retrieve_context(duplicate, "Шаги")) == 2


def test_focused_question_remains_small_with_larger_top_k():
    results = [(10, c(str(i), 3)) for i in range(8)]
    assert (
        retrieve_context(Index(results), "График работы?", top_k=6)
        == results[:2]
    )
    assert len(retrieve_context(Index(results), "Опиши шаги", top_k=6)) == 6


def test_empty_and_invalid_limits():
    assert retrieve_context(Index([]), "Шаги") == []
    with pytest.raises(ValueError):
        retrieve_context(Index([]), "Шаги", max_chars=0)


def test_chunk_count_is_bounded():
    results = [(10, c(str(i), 7)) for i in range(12)]
    assert len(retrieve_context(Index(results), "Шаги", max_chunks=4)) == 4


def test_no_supplements_if_neither_seed_fits():
    results = [
        (10, c("a", 7, text="x" * 1000)),
        (9, c("b", 8, text="y" * 1000)),
        (8, c("c", 9)),
    ]
    assert retrieve_context(Index(results), "Шаги", max_chars=100) == []
