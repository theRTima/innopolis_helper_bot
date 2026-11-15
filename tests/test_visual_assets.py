import pytest

from app.ingestion.assets import register_asset, verify_assets
from app.ingestion.normalized import NormalizedDocument, finalize_chunks
from app.rag.index import _read_chunk_file


def visual_document(tmp_path):
    image_root = tmp_path / "images"
    image_root.mkdir()
    image = image_root / "diagram.png"
    image.write_bytes(b"test-image-bytes")
    nd = NormalizedDocument()
    register_asset(nd.assets, "a1", image, image_root, 4, [1, 2, 3, 4])
    element = nd.add_structured(
        element_type="diagram",
        text="Схема согласования",
        data={},
        page_from=4,
        asset_ids=["a1"],
        source_regions=[{"page": 4, "bbox": [1, 2, 3, 4]}],
    )
    return nd, element


META = dict(
    source_name="travel.pdf",
    document_id="travel",
    version_id="v1",
    source_sha256="test",
    artifact="original.pdf",
)


def test_resolved_and_fallback_chunks_keep_visual_evidence(tmp_path):
    nd, element = visual_document(tmp_path)
    resolved = nd.resolve_chunks(
        [
            {
                "type": "diagram",
                "source_reference_ids": element.source_reference_ids,
            }
        ],
        **META,
    )
    fallback = nd.fallback_chunks(**META)
    for chunks in (resolved, fallback):
        chunks = finalize_chunks(chunks, document_id="travel", version_id="v1")
        assert chunks[0]["asset_ids"] == ["a1"]
        assert chunks[0]["element_ids"] == [element.element_id]
        assert chunks[0]["assets"][0]["path"] == "images/diagram.png"
        assert chunks[0]["source_regions"][0]["page"] == 4
        verify_assets(tmp_path, chunks[0]["assets"])


def test_omitted_visual_source_retains_asset_in_recovery(tmp_path):
    nd, element = visual_document(tmp_path)
    body = nd.add_text(text="Обычный текст инструкции", page_from=7)
    chunks = nd.resolve_chunks(
        [
            {
                "source_reference_ids": body.source_reference_ids,
            }
        ],
        **META,
    )
    visual = next(chunk for chunk in chunks if chunk["type"] == "diagram")
    assert visual["asset_ids"] == ["a1"]


def test_asset_integrity_and_path_boundaries(tmp_path):
    nd, _ = visual_document(tmp_path)
    assets = list(nd.assets.values())
    (tmp_path / "images/diagram.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        verify_assets(tmp_path, assets)
    with pytest.raises(ValueError, match="escapes"):
        verify_assets(tmp_path, [{"path": "../escape", "sha256": "none"}])
    with pytest.raises(ValueError, match="inside"):
        register_asset(
            {}, "bad", tmp_path / "external.png", tmp_path / "images", 1
        )


def test_loader_keeps_assets_and_legacy_chunks(tmp_path):
    import json

    base = {"id": "one", "source": "test.pdf", "page": 1, "text": "Text"}
    path = tmp_path / "chunks.jsonl"
    path.write_text(json.dumps(base) + "\n")
    assert _read_chunk_file(path)[0].assets == ()
    base.update(asset_ids=["a1"], assets=[{"asset_id": "a1"}])
    path.write_text(json.dumps(base) + "\n")
    assert _read_chunk_file(path)[0].asset_ids == ("a1",)


def test_text_merge_does_not_drop_evidence():
    chunks = [
        dict(
            id="a",
            type="text",
            page=1,
            title="Section",
            content="first part",
            data=None,
            asset_ids=["a1"],
        ),
        dict(
            id="b",
            type="text",
            page=1,
            title="Section",
            content="second part",
            data=None,
            asset_ids=["a2"],
        ),
    ]
    result = finalize_chunks(chunks, document_id="doc", version_id="v1")
    assert result[0]["asset_ids"] == ["a1", "a2"]


def test_extraction_publishes_images_and_checks_cache(tmp_path, monkeypatch):
    """Exercise actual persistence with parser/model boundaries replaced."""
    import importlib.util
    import sys
    import types
    from pathlib import Path
    from PIL import Image
    import app.ingestion
    from rag_pipeline.ingest import ingest_document

    fake = types.ModuleType("app.ingestion.new_pipeline")
    vision = types.ModuleType("app.ingestion.vision_parser")

    def build(image_path, **kwargs):
        import shutil

        nd = NormalizedDocument()
        root = kwargs["temp_dir"]
        target = root / "diagram.png"
        shutil.copyfile(image_path, target)
        register_asset(nd.assets, "a1", target, root, 1)
        nd.add_structured(
            element_type="image",
            text="Saved image",
            data=None,
            page_from=1,
            asset_ids=["a1"],
        )
        return nd, []

    fake.build_from_image = build
    fake.chunk_elements = lambda *args, **kwargs: ([], [])
    for name, module in (("new_pipeline", fake), ("vision_parser", vision)):
        monkeypatch.setitem(sys.modules, "app.ingestion." + name, module)
        monkeypatch.setattr(app.ingestion, name, module, raising=False)
    path = Path(__file__).parents[1] / "rag_pipeline/extract.py"
    spec = importlib.util.spec_from_file_location("isolated_extract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "configuration", lambda: {"pipeline_version": 2}
    )
    image = tmp_path / "source.png"
    Image.new("RGB", (20, 30), "blue").save(image)
    stored = ingest_document(image, "image", tmp_path / "store")
    first = module.extract_version(stored["version_path"])
    output = Path(first["chunk_path"])
    asset = first["manifest"]["assets"][0]
    assert "quality" in first["manifest"]
    assert first["manifest"]["quality"]["missing_source_ids"] == []
    assert (output / asset["path"]).read_bytes() == image.read_bytes()
    import json

    evidence = json.loads((output / "normalized.json").read_text())
    assert evidence["elements"][0]["asset_ids"] == ["a1"]
    assert module.extract_version(stored["version_path"])["cached"]
    (output / asset["path"]).write_bytes(b"broken")
    with pytest.raises(ValueError, match="checksum"):
        module.extract_version(stored["version_path"])
