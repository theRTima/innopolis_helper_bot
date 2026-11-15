"""Shared, offline-testable rendering of the actual LLM text context."""

from app.rag.index import Chunk


def format_context(results: list[tuple[float, Chunk]]) -> str:
    """Keep source numbering stable and put the best result last."""
    blocks = []
    for position in range(len(results) - 1, -1, -1):
        chunk = results[position][1]
        label = f"[{position + 1}] {chunk.document}, стр. {chunk.page_label}"
        if chunk.title:
            label += f" — {chunk.title}"
        blocks.append(f"{label}\n{chunk.text}")
    return "\n\n".join(blocks)
