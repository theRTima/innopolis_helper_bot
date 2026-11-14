from app.bot.handlers import _format_sources
from app.llm.client import _format_context
from app.rag.index import Chunk


def _chunk(chunk_id: str, page: int, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document="Документ",
        page_from=page,
        page_to=page,
        text=text,
    )


def test_best_chunk_goes_last_with_stable_label() -> None:
    results = [
        (2.0, _chunk("best", 7, "нужный ответ")),
        (1.0, _chunk("noise", 14, "нерелевантный фрагмент")),
    ]

    context = _format_context(results)

    assert context.index("[2]") < context.index("[1]")
    assert "[1] Документ, стр. 7" in context


def test_sources_use_same_numbering() -> None:
    results = [
        (2.0, _chunk("best", 7, "ответ")),
        (1.0, _chunk("noise", 14, "шум")),
    ]

    sources = _format_sources(results)

    assert "[1] Документ, стр. 7" in sources
    assert "[2] Документ, стр. 14" in sources


def test_sources_only_include_cited_positions() -> None:
    results = [(1.0, _chunk("best", 7, "ответ")), (0.5, _chunk("noise", 14, "шум"))]

    assert _format_sources(results, {1}) == "\n\nИсточники:\n[1] Документ, стр. 7"
