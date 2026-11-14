"""Checkpoint 1: immutable PDF versions with filesystem metadata."""

import argparse
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader


class IngestionError(Exception):
    """An input, storage, or integrity error understandable by a CLI user."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_pdf(path: Path) -> int:
    try:
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise IngestionError("File does not start with a PDF header.")
            stream.seek(0)
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                raise IngestionError("Encrypted PDFs are not supported; provide an unencrypted copy.")
            count = len(reader.pages)
            if count == 0:
                raise IngestionError("PDF contains no pages.")
            # Visit each page and decode its contents, without OCR or text extraction.
            for page in reader.pages:
                contents = page.get_contents()
                if contents is not None:
                    contents.get_data()
            return count
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError(f"PDF could not be read: {exc}") from exc


def ingest_pdf(
    path: str | Path,
    document_id: str,
    storage_root: str | Path = "data/processed/documents",
) -> dict:
    """Save a PDF snapshot. Deduplication is scoped to document_id.

    A full content hash is the version ID. Each manifest describes a stored,
    validated upload, not an indexed or extraction-ready document.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", document_id):
        raise IngestionError("document_id must contain 1-80 lowercase ASCII letters, digits, '-' or '_', starting with a letter or digit.")
    if document_id.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise IngestionError("document_id is a reserved Windows filename.")
    source = Path(path).resolve()
    if not source.is_file():
        raise IngestionError(f"Source file does not exist: {source}")
    document_dir = Path(storage_root).resolve() / document_id
    document_dir.mkdir(parents=True, exist_ok=True)
    lock = document_dir / ".ingest.lock"
    try:
        lock_stream = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise IngestionError(f"Document ingestion is locked: {lock}. If a previous process crashed, remove it only after checking no ingestion is running.") from exc
    try:
        with lock_stream:
            lock_stream.write(datetime.now(timezone.utc).isoformat())
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=document_dir) as tmp:
            stage = Path(tmp)
            original = stage / "original.pdf"
            shutil.copyfile(source, original)
            # Validate the same snapshot that will be stored, even if input changes.
            page_count = validate_pdf(original)
            digest = sha256_file(original)
            version_id = "sha256-" + digest
            destination = document_dir / version_id
            if destination.exists():
                try:
                    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
                    valid = (
                        manifest["schema_version"] == 1
                        and manifest["document_id"] == document_id
                        and manifest["version_id"] == version_id
                        and manifest["sha256"] == digest
                        and manifest["page_count"] == page_count
                        and manifest["status"] == "uploaded"
                        and sha256_file(destination / "original.pdf") == digest
                    )
                    if not valid:
                        raise ValueError("stored manifest or original does not match")
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise IngestionError(f"Existing version has an integrity error: {destination}") from exc
                return {"duplicate": True, "version_path": str(destination), "manifest": manifest}
            manifest = {
                "schema_version": 1,
                "document_id": document_id,
                "version_id": version_id,
                "original_filename": source.name,
                "sha256": digest,
                "byte_size": original.stat().st_size,
                "page_count": page_count,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "uploaded",
                "original_path": "original.pdf",
            }
            (stage / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            # Move the complete directory on the same filesystem in one operation.
            stage.rename(destination)
            return {"duplicate": False, "version_path": str(destination), "manifest": manifest}
    finally:
        lock.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and store an immutable PDF version (checkpoint 1).")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--document-id", required=True, help="Stable logical ID; reuse for revised versions of this document")
    parser.add_argument("--storage-root", type=Path, default=Path("data/processed/documents"))
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        result = ingest_pdf(args.pdf, args.document_id, args.storage_root)
    except (IngestionError, OSError) as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
