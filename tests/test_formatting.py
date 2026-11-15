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
