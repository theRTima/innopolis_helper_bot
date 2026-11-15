"""Inspect existing retrieval inputs without importing or calling a model."""

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

from app.rag.context import format_context
from app.rag.index import SearchIndex, load_chunks_auto
from app.rag.representation import query_representation, search_representation
from app.rag.retrieve import retrieve_context


def inspect_retrieval(
    documents_root,
    chunks_dir,
    question,
    top_k=2,
    min_score=0.0,
    expand=False,
):
    chunks, mode = load_chunks_auto(Path(chunks_dir), Path(documents_root))
    index = SearchIndex(chunks)
    results = (
        retrieve_context(index, question, top_k=top_k, min_score=min_score)
        if expand
        else index.search(
            question,
            top_k=top_k,
            min_score=min_score,
        )
    )
    return {
        "mode": mode,
        "documents_root": str(Path(documents_root).resolve()),
        "chunk_count": len(chunks),
        "types": dict(Counter(chunk.chunk_type for chunk in chunks)),
        "selected_files": sorted({chunk.chunk_file for chunk in chunks}),
        "question": question,
        "search_query": query_representation(question),
        "top_k": top_k,
        "expanded": expand,
        "context_characters": len(format_context(results)),
        "min_score": min_score,
        "results": [
            {
                "rank": rank,
                "score": score,
                "chunk_id": chunk.chunk_id,
                "type": chunk.chunk_type,
                "origin": chunk.origin,
                "document_id": chunk.document_id,
                "version_id": chunk.version_id,
                "page": chunk.page_label,
                "chunk_file": chunk.chunk_file,
                "text": chunk.text,
                "search_representation": search_representation(chunk),
            }
            for rank, (score, chunk) in enumerate(results, 1)
        ],
        "llm_context": format_context(results),
        "model_called": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question")
    parser.add_argument(
        "--documents-root", type=Path, default=Path("data/processed/documents")
    )
    parser.add_argument(
        "--chunks-dir", type=Path, default=Path("data/processed")
    )
    parser.add_argument("--top-k", type=int, default=2)
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expand", action="store_true")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    logging.basicConfig(level=logging.INFO)
    report = inspect_retrieval(
        args.documents_root,
        args.chunks_dir,
        args.question,
        args.top_k,
        args.min_score,
        args.expand,
    )
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
