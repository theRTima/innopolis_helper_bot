"""Exercise review gates and recovery with no parser/model dependencies."""

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from app.rag.extraction import normalized as norm
from app.rag.extraction.quality import (
    audit_analysis,
    coverage_report,
    should_fallback,
)


def actual_add_items():
    """Load real pure conversion functions without importing Docling/client."""
    source = Path(__file__).parents[1] / "app/rag/extraction/new_pipeline.py"
    names = {
        "_text_from_transcription",
        "_text_from_observations",
        "_table_markdown",
        "_diagram_text",
        "analysis_item_text",
        "analysis_item_data",
        "_add_analysis_items",
    }
    module = ast.parse(source.read_text(encoding="utf-8"))
    module.body = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {
        "norm": norm,
        "audit_analysis": audit_analysis,
        "defaultdict": defaultdict,
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_add_analysis_items"]


def analysis():
    return {
        "status": "complete",
        "items": [
            {
                "local_id": "v1",
                "type": "image",
                "transcription": [{"text": "Label", "readability": "clear"}],
            }
        ],
        "proposals": [],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "partial"),
        ("conflicts", [{"issue": "wrong label"}]),
        ("unresolved", [{"issue": "unknown arrow"}]),
        ("limitations", ["cropped"]),
        (
            "proposals",
            [
                {
                    "operation": "replace_candidate",
                    "item_ids": ["v1"],
                    "review_required": True,
                }
            ],
        ),
    ],
)
def test_unsafe_analysis_is_retained_but_not_published(field, value):
    data = analysis()
    data[field] = value
    nd = norm.NormalizedDocument()
    added = actual_add_items()(
        nd,
        data,
        page=4,
        section_path="",
        parent_section_id=None,
        asset_id="a1",
        bbox=None,
        raw_response="original response",
    )
    assert added == 0
    assert not nd.sources
    assert nd.analysis_records[0]["raw_response"] == "original response"
    assert nd.analysis_records[0]["analysis"] == data
    assert coverage_report(nd, [], nd.warnings)["status"] == "needs_review"


def test_clear_analysis_is_accepted_without_claiming_ground_truth():
    nd = norm.NormalizedDocument()
    actual_add_items()(
        nd,
        analysis(),
        page=4,
        section_path="",
        parent_section_id=None,
        asset_id="a1",
        bbox=None,
    )
    assert len(nd.sources) == 1
    assert nd.analysis_records[0]["quality_status"] == "accepted"


def test_uncertain_edge_and_transcription_are_quarantined():
    data = analysis()
    data["items"][0]["diagram"] = {
        "edges": [{"direction_status": "uncertain"}]
    }
    assert not audit_analysis(data, "", 4, "a1")[0]
    data = analysis()
    data["items"][0]["transcription"][0]["readability"] = "unreadable"
    assert not audit_analysis(data, "", 4, "a1")[0]


def test_failed_picture_triggers_fallback_even_with_heading():
    assert should_fallback(
        failed_visual=True,
        has_elements=True,
        body_chars=1000,
        has_visual=True,
        min_page_text=50,
    )
    assert not should_fallback(
        failed_visual=False,
        has_elements=True,
        body_chars=1000,
        has_visual=True,
        min_page_text=50,
    )


META = dict(
    source_name="doc.pdf",
    document_id="doc",
    version_id="v1",
    source_sha256="test",
    artifact="original.pdf",
)


def test_empty_selection_does_not_suppress_source_recovery():
    nd = norm.NormalizedDocument()
    element = nd.add_structured(
        element_type="table",
        text="Header\nimportant value",
        data={"columns": ["Header"], "rows": [["important value"]]},
        page_from=1,
    )
    chunks = nd.resolve_chunks(
        [
            {
                "source_reference_ids": element.source_reference_ids,
                "selectors": [
                    {
                        "source_reference_id": element.source_reference_ids[0],
                        "kind": "table_rows",
                        "start": 99,
                        "end_exclusive": 99,
                        "include_header_rows": [99],
                    }
                ],
            }
        ],
        **META,
    )
    assert any("important value" in chunk["content"] for chunk in chunks)


def test_no_valid_model_chunks_still_recovers_text():
    nd = norm.NormalizedDocument()
    element = nd.add_text(text="Important instruction", page_from=1)
    chunks = nd.resolve_chunks(
        [{"source_reference_ids": ["nonexistent"]}], **META
    )
    assert chunks[0]["content"] == "Important instruction"
    assert element.source_reference_ids[0] in chunks[0]["source_reference_ids"]
    report = coverage_report(nd, chunks, nd.warnings)
    assert report["missing_source_ids"] == []
    assert report["warning_count"] > 0


def test_missing_source_is_visible_in_quality_report():
    nd = norm.NormalizedDocument()
    element = nd.add_text(text="Lost", page_from=1)
    report = coverage_report(nd, [], [])
    assert report["missing_source_ids"] == element.source_reference_ids


def test_empty_resolved_chunk_still_recovers_primary_source(monkeypatch):
    nd = norm.NormalizedDocument()
    element = nd.add_text(text="Must not disappear", page_from=1)
    monkeypatch.setattr(nd, "_selected_text", lambda *args: "")
    chunks = nd.resolve_chunks(
        [
            {
                "source_reference_ids": element.source_reference_ids,
            }
        ],
        **META,
    )
    assert chunks[0]["content"] == "Must not disappear"
    assert coverage_report(nd, chunks, [])["missing_source_ids"] == []


def test_partial_table_selection_restores_unselected_rows():
    nd = norm.NormalizedDocument()
    element = nd.add_structured(
        element_type="table",
        text="Header\nImportant row",
        data={"columns": ["Header"], "rows": [["Important row"]]},
        page_from=1,
    )
    chunks = nd.resolve_chunks(
        [
            {
                "source_reference_ids": element.source_reference_ids,
                "selectors": [
                    {
                        "source_reference_id": element.source_reference_ids[0],
                        "kind": "table_rows",
                        "start": 0,
                        "end_exclusive": 1,
                    }
                ],
            }
        ],
        **META,
    )
    assert len(chunks) == 2
    assert "Important row" not in chunks[0]["content"]
    assert "Important row" in chunks[1]["content"]
