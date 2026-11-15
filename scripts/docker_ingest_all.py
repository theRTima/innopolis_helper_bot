"""Ingest every PDF in the knowledge directory through the versioned pipeline.

The Docker ``ingest`` service uses this script. Add or replace PDFs in
``data/knowledge`` and rerun the service; no ``.env`` changes are required.

Document IDs:
- if a stored manifest already uses the same source filename, that document ID
  is reused (so existing versions keep their history);
- otherwise a stable ID is generated from the filename (Cyrillic is
  transliterated to ASCII);
- collisions get a short hash suffix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.extract import extract_version
from app.rag.ingest import ingest_document
from app.rag.render import render_version

CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya", "і": "i", "ї": "yi",
    "є": "ye", "ґ": "g",
}


def slugify(value: str, *, max_length: int = 60) -> str:
    parts = []
    for char in value.casefold():
        if "a" <= char <= "z" or "0" <= char <= "9":
            parts.append(char)
        elif char in CYRILLIC:
            parts.append(CYRILLIC[char])
        else:
            parts.append("-")
    slug = re.sub(r"-+", "-", "".join(parts)).strip("-")
    if not slug or not slug[0].isalnum():
        slug = f"doc-{slug}".strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "document"


def existing_document_ids(storage_root: Path) -> dict[str, str]:
    """Map original filenames to already stored document IDs."""
    mapping: dict[str, tuple[str, str]] = {}
    for manifest_path in storage_root.glob("*/sha256-*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        filename = manifest.get("original_filename")
        document_id = manifest.get("document_id")
        if not isinstance(filename, str) or not isinstance(document_id, str):
            continue
        created = str(manifest.get("created_at", ""))
        key = filename.casefold()
        current = mapping.get(key)
        if current is None or created > current[0]:
            mapping[key] = (created, document_id)
    return {key: value[1] for key, value in mapping.items()}


def document_id_for(
    source: Path,
    knowledge_dir: Path,
    existing: dict[str, str],
    used: set[str],
) -> str:
    known = existing.get(source.name.casefold())
    if known and known not in used:
        used.add(known)
        return known

    try:
        relative = source.relative_to(knowledge_dir)
    except ValueError:
        relative = Path(source.name)
    base = slugify(str(relative.with_suffix("")))
    candidate = base
    if candidate in used or (known and candidate != known):
        digest = hashlib.sha256(
            str(relative).encode("utf-8")
        ).hexdigest()[:8]
        candidate = f"{base[:60]}-{digest}"
    used.add(candidate)
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=Path("data/knowledge"),
    )
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=Path("data/processed/documents"),
    )
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--target-tokens", type=int, default=600)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show derived document IDs without ingesting",
    )
    args = parser.parse_args()

    knowledge_dir = args.knowledge_dir.resolve()
    storage_root = args.storage_root.resolve()
    if not knowledge_dir.is_dir():
        print(f"Knowledge directory does not exist: {knowledge_dir}", file=sys.stderr)
        return 1

    pdfs = sorted(
        path
        for path in knowledge_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() == ".pdf"
    )
    if not pdfs:
        print(f"No PDF files found in {knowledge_dir}", file=sys.stderr)
        return 1

    existing = existing_document_ids(storage_root)
    used: set[str] = set()
    plan = []
    for source in pdfs:
        document_id = document_id_for(source, knowledge_dir, existing, used)
        plan.append((source, document_id))
        relative = source.relative_to(knowledge_dir)
        print(f"{relative}: document_id={document_id}")

    if args.dry_run:
        print(json.dumps({"dry_run": True, "pdf_count": len(plan)}, indent=2))
        return 0

    results = []
    errors = []
    for source, document_id in plan:
        try:
            ingested = ingest_document(
                source,
                document_id,
                storage_root=storage_root,
            )
            version_path = Path(ingested["version_path"])
            rendered = render_version(version_path, dpi=args.dpi)
            extracted = extract_version(
                version_path,
                target_tokens=args.target_tokens,
            )
            manifest = extracted.get("manifest") or {}
            results.append(
                {
                    "source": str(source.relative_to(knowledge_dir)),
                    "document_id": document_id,
                    "version_path": str(version_path),
                    "duplicate": ingested.get("duplicate", False),
                    "render_cached": rendered.get("cached", False),
                    "chunk_cached": extracted.get("cached", False),
                    "chunk_count": manifest.get("chunk_count"),
                    "quality": (manifest.get("quality") or {}).get("status"),
                }
            )
        except Exception as error:
            errors.append(
                {
                    "source": str(source.relative_to(knowledge_dir)),
                    "document_id": document_id,
                    "error": str(error),
                }
            )

    print(
        json.dumps(
            {
                "pdf_count": len(plan),
                "processed": results,
                "errors": errors,
                "status": "partial_failure" if errors else "ready",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
