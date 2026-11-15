"""Tests for loading chunks from the versioned app.app/rag store."""

from __future__ import annotations

import json
from pathlib import Path

from app.rag.index import SearchIndex, load_versioned_chunks


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_chunks(path: Path, chunks: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")


def _version(
    root: Path,
    document_id: str,
    version_id: str,
    created_at: str,
    chunks: list[dict],
    *,
    status: str = "ready",
) -> Path:
    version = root / document_id / version_id
    _write_json(
        version / "manifest.json",
        {
            "schema_version": 2,
            "document_id": document_id,
            "version_id": version_id,
            "original_filename": f"{document_id}.pdf",
            "sha256": version_id.replace("sha256-", ""),
            "created_at": created_at,
            "status": "uploaded",
            "format": "pdf",
            "original_path": "original.pdf",
        },
    )
    chunk_dir = version / "chunks" / "chunk-test"
    _write_json(
        chunk_dir / "chunk_manifest.json",
        {
            "schema_version": 1,
            "status": status,
            "created_at": created_at,
        },
    )
    _write_chunks(chunk_dir / "chunks.jsonl", chunks)
    return version


def _chunk(chunk_id: str, source: str = "doc.pdf") -> dict:
    return {
        "id": chunk_id,
        "source": source,
        "document_id": "doc",
        "version_id": "sha256-v2",
        "page": 3,
        "chunk_index": 0,
        "origin": "docling",
        "type": "text",
        "title": "Section",
        "content": "Text",
        "data": None,
        "text": "Section\nText",
        "locator": {"kind": "pdf-page", "value": 3},
    }


def test_versioned_loader_uses_latest_ready_version(tmp_path):
    root = tmp_path / "documents"
    _version(
        root,
        "doc",
        "sha256-v1",
        "2026-01-01T00:00:00+00:00",
        [_chunk("old")],
    )
    _version(
        root,
        "doc",
        "sha256-v2",
        "2026-02-01T00:00:00+00:00",
        [_chunk("new")],
    )
    _version(
        root,
        "doc",
        "sha256-v3",
        "2026-03-01T00:00:00+00:00",
        [_chunk("partial")],
        status="partial",
    )

    chunks = load_versioned_chunks(root)
    assert [chunk.chunk_id for chunk in chunks] == ["new"]
    assert chunks[0].document == "doc"
    assert chunks[0].document_id == "doc"
    assert chunks[0].version_id == "sha256-v2"
    assert chunks[0].locator == {"kind": "pdf-page", "value": 3}


def test_search_index_refreshes_after_document_import(tmp_path):
    root = tmp_path / "documents"
    index = SearchIndex([], documents_root=root)
    _version(
        root,
        "new-document",
        "sha256-new",
        "2026-11-15T00:00:00+00:00",
        [{**_chunk("new-chunk"), "text": "Unique furniture inventory information"}],
    )

    assert index.refresh() is True
    assert index.search("furniture inventory")
    assert index.refresh() is False
