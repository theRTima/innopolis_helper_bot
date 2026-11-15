"""Compare lexical retrieval against the legacy baseline without a model."""

import argparse
import json
from pathlib import Path

from app.rag.index import SearchIndex, load_versioned_chunks


def evaluate(documents_root, cases_path, top_k=2):
    specification = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    chunks = load_versioned_chunks(Path(documents_root))
    if not chunks:
        raise ValueError("No ready chunks found")
    if not any(c.document_id == specification["document_id"] for c in chunks):
        raise ValueError("Evaluation document is missing from the corpus")
    output = {
        "model_called": False,
        "top_k": top_k,
        "scope": "Page/group retrieval proxies, not answer correctness",
        "selected_files": sorted({c.chunk_file for c in chunks}),
        "cases": [],
    }
    indices = {
        name: SearchIndex(chunks, use_search_representation=enabled)
        for name, enabled in (("baseline", False), ("enhanced", True))
    }
    for case in specification["cases"]:
        row = {"id": case["id"], "question": case["question"]}
        for name, index in indices.items():
            results = index.search(case["question"], top_k=len(chunks))
            relevant = [
                (rank, chunk)
                for rank, (_, chunk) in enumerate(results, 1)
                if chunk.document_id == specification["document_id"]
            ]
            groups = case["evidence_groups"]
            primary_rank = next(
                (
                    rank
                    for rank, chunk in relevant
                    if chunk.page_from in groups[0]
                ),
                None,
            )
            covered = sum(
                any(
                    rank <= top_k and chunk.page_from in group
                    for rank, chunk in relevant
                )
                for group in groups
            )
            row[name] = {
                "primary_rank": primary_rank,
                "primary_hit": primary_rank is not None
                and primary_rank <= top_k,
                "evidence_group_coverage": covered / len(groups),
                "results": [
                    {
                        "id": c.chunk_id,
                        "document_id": c.document_id,
                        "page": c.page_from,
                        "type": c.chunk_type,
                        "score": score,
                    }
                    for score, c in results[:top_k]
                ],
            }
        output["cases"].append(row)
    output["primary_hits"] = {
        name: sum(row[name]["primary_hit"] for row in output["cases"])
        for name in indices
    }
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents-root", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    result = evaluate(args.documents_root, args.cases, args.top_k)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["primary_hits"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
