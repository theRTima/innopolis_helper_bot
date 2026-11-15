import hashlib
import json

import pytest

from app.rag.inspect import inspect_retrieval
from app.rag.index import load_versioned_chunks
from test_versioned_index import _chunk, _version


def test_report_uses_real_context_and_selected_file(tmp_path):
    root = tmp_path / "documents"
    chunk = _chunk("diagram")
    chunk.update(type="diagram", origin="vision", text="Схема согласования")
    version = _version(root, "doc", "sha256-v2", "2026-02-01", [chunk])
    report = inspect_retrieval(root, tmp_path / "empty", "схема")
    assert report["model_called"] is False
    assert report["types"] == {"diagram": 1}
    assert report["results"][0]["chunk_id"] == "diagram"
    assert (
        "[1] doc, стр. 3 — Section\nСхема согласования"
        == report["llm_context"]
    )
    assert report["selected_files"] == [
        str((version / "chunks/chunk-test/chunks.jsonl").resolve())
    ]


def test_hash_mismatch_is_not_silently_loaded(tmp_path):
    version = _version(
        tmp_path, "doc", "sha256-v2", "2026-02-01", [_chunk("one")]
    )
    path = version / "chunks/chunk-test/chunk_manifest.json"
    manifest = json.loads(path.read_text())
    data = path.parent / "chunks.jsonl"
    manifest["chunks_sha256"] = hashlib.sha256(data.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    assert len(load_versioned_chunks(tmp_path)) == 1
    data.write_text(data.read_text() + "\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_versioned_chunks(tmp_path)


def test_missing_latest_jsonl_does_not_hide_valid_set(tmp_path):
    version = _version(
        tmp_path, "doc", "sha256-v2", "2026-02-01", [_chunk("one")]
    )
    path = version / "chunks/newer"
    path.mkdir()
    (path / "chunk_manifest.json").write_text(
        json.dumps({"status": "ready", "created_at": "2026-03-01"})
    )
    assert load_versioned_chunks(tmp_path)[0].chunk_id == "one"
