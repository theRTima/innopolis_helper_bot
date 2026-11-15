"""Run the full versioned pipeline for one source file.

Used by the Docker ``ingest`` profile. It performs:

    ingest -> render -> extract

The source and document ID are passed as arguments; the result is printed as
JSON so the caller can inspect the version and chunk paths.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.extract import extract_version
from app.rag.ingest import ingest_document
from app.rag.render import render_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=Path("data/processed/documents"),
    )
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument(
        "--review-markdown",
        action="store_true",
        help="Also generate optional model-written review Markdown",
    )
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.is_file():
        print(f"Source file does not exist: {source}", file=sys.stderr)
        return 1

    try:
        ingested = ingest_document(
            source,
            args.document_id,
            storage_root=args.storage_root,
        )
        version_path = Path(ingested["version_path"])
        rendered = render_version(version_path, dpi=args.dpi)
        extracted = extract_version(
            version_path,
            target_tokens=args.target_tokens,
            review_markdown=args.review_markdown,
        )
    except Exception as error:
        print(f"Pipeline failed: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "document_id": args.document_id,
                "version_path": str(version_path),
                "ingest_duplicate": ingested.get("duplicate", False),
                "render_cached": rendered.get("cached", False),
                "chunk_cached": extracted.get("cached", False),
                "chunk_path": extracted.get("chunk_path"),
                "chunk_manifest": extracted.get("manifest"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
