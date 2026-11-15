"""Tests for checkpoint 3 extraction of a stored text version."""

from __future__ import annotations

import json
from pathlib import Path

from app.rag.extract import extract_version


def _text_version(tmp_path: Path) -> Path:
    version = tmp_path / "docs" / "notes" / "sha256-test"
    version.mkdir(parents=True)
    original = version / "original.txt"
    original.write_text("Первая строка\nВторая строка\n", encoding="utf-8")
    manifest = {
        "schema_version": 2,
        "document_id": "notes",
        "version_id": "sha256-test",
        "original_filename": "notes.txt",
        "sha256": __import__("hashlib").sha256(original.read_bytes()).hexdigest(),
        "byte_size": original.stat().st_size,
        "format": "text",
        "source_kind": "text",
        "page_count": None,
        "validation": {"encoding": "utf-8", "line_count": 2},
        "created_at": "2026-01-01T00:00:00+00:00",
        "status": "uploaded",
        "original_path": "original.txt",
    }
    (version / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return version


def test_text_extraction_writes_manifest_and_caches(tmp_path):
    version = _text_version(tmp_path)
    first = extract_version(version)
    assert first["cached"] is False
    manifest = first["manifest"]
    assert manifest["status"] == "ready"
    assert manifest["document_id"] == "notes"
    assert manifest["chunk_count"] == 1
    assert first["stats"]["model_calls"] == 0
    assert manifest["model_stats"]["model_calls"] == 0

    chunk_dir = Path(first["chunk_path"])
    assert (chunk_dir / "chunks.jsonl").is_file()
    assert (chunk_dir / "chunks.md").is_file()
    assert (chunk_dir / "chunk_manifest.json").is_file()
    assert not (chunk_dir / "images").exists()

    chunks = [
        json.loads(line)
        for line in (chunk_dir / "chunks.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    assert chunks[0]["document_id"] == "notes"
    assert chunks[0]["version_id"] == "sha256-test"
    assert chunks[0]["source"] == "notes.txt"
    assert "Первая строка" in chunks[0]["content"]

    second = extract_version(version)
    assert second["cached"] is True
    assert second["chunk_path"] == first["chunk_path"]
