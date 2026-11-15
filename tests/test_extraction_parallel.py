"""Tests for bounded parallel extraction helpers."""

import json
import re
from dataclasses import replace
from functools import partial
from threading import Barrier, BrokenBarrierError

import pytest
from PIL import Image

from app.rag.extraction import new_pipeline as np
from app.rag.extraction import vision_parser as vp


def test_parallel_map_preserves_order_and_runs_concurrently():
    barrier = Barrier(2, timeout=5)

    def task(value):
        try:
            barrier.wait()
        except BrokenBarrierError:
            raise AssertionError("tasks did not run concurrently")
        return value

    results, errors = np._parallel_map(
        [partial(task, index) for index in range(2)], 2
    )
    assert errors == {}
    assert results == [0, 1]


def test_parallel_map_returns_errors_without_losing_other_results():
    def task(value):
        if value == 1:
            raise RuntimeError("failed")
        return value

    results, errors = np._parallel_map(
        [partial(task, index) for index in range(3)], 3
    )
    assert results == [0, None, 2]
    assert list(errors) == [1]
    assert str(errors[1]) == "failed"


def test_valid_empty_analysis_does_not_trigger_page_fallback():
    assert np._analysis_is_empty({"status": "empty", "items": []})
    assert not np._analysis_is_empty({"status": "empty", "items": [{}]})
    assert not np._analysis_is_empty({"status": "partial", "items": []})
    assert not np._analysis_is_empty({})


def test_tiny_picture_filter_uses_pixel_area(tmp_path, monkeypatch):
    monkeypatch.setattr(
        np.vp,
        "SETTINGS",
        replace(np.vp.SETTINGS, extraction_min_image_pixels=1024),
    )
    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (20, 20), "white").save(tiny)
    large = tmp_path / "large.png"
    Image.new("RGB", (100, 100), "white").save(large)

    assert np._picture_too_small(tiny)
    assert not np._picture_too_small(large)


def test_vision_cache_key_is_stable_and_input_sensitive():
    first = vp._cache_key("vision", "prompt", b"image")
    assert first == vp._cache_key("vision", "prompt", b"image")
    assert first != vp._cache_key("vision", "other prompt", b"image")
    assert first != vp._cache_key("vision", "prompt", b"other image")
    assert first != vp._cache_key("text", "prompt", b"image")


def test_parallel_chunking_keeps_page_order(monkeypatch):
    monkeypatch.setattr(
        np.vp,
        "SETTINGS",
        replace(np.vp.SETTINGS, extraction_text_concurrency=2),
    )
    barrier = Barrier(2, timeout=5)

    def fake_run_text(prompt):
        element_id = re.search(r'"element_id": "(e\d+)"', prompt).group(1)
        try:
            barrier.wait()
        except BrokenBarrierError:
            raise AssertionError("chunker calls did not run concurrently")
        return json.dumps({"chunks": [{"element_ids": [element_id]}]})

    monkeypatch.setattr(np, "run_text", fake_run_text)
    elements = [
        {"element_id": "e1", "source_regions": [{"page": 1}]},
        {"element_id": "e2", "source_regions": [{"page": 2}]},
    ]
    chunks, warnings = np.chunk_elements(
        elements, document_id="doc", version_id="v1"
    )
    assert warnings == []
    assert [chunk["element_ids"][0] for chunk in chunks] == ["e1", "e2"]


def test_vision_cache_reuses_raw_response(tmp_path, monkeypatch):
    monkeypatch.setattr(
        vp,
        "SETTINGS",
        replace(
            vp.SETTINGS,
            extraction_cache_enabled=True,
            extraction_cache_dir=tmp_path,
        ),
    )
    calls = []

    def fake_chat(messages):
        calls.append(messages)
        return '{"ok": true}'

    monkeypatch.setattr(vp, "_chat_text", fake_chat)
    image = tmp_path / "image.png"
    image.write_bytes(b"image-bytes")
    vp.reset_stats()

    assert vp.run_vision("prompt", image) == '{"ok": true}'
    assert vp.run_vision("prompt", image) == '{"ok": true}'
    assert len(calls) == 1
    stats = vp.get_stats()
    assert stats["vision_calls"] == 1
    assert stats["cache_hits"] == 1

    assert vp.run_vision("another prompt", image) == '{"ok": true}'
    assert len(calls) == 2
