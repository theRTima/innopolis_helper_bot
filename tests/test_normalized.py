"""Tests for normalized elements and reference-based chunk resolution."""

from __future__ import annotations

from app.rag.extraction import normalized as norm
from app.rag.extraction import vision_parser as vp


def _nd_with_sources():
    nd = norm.NormalizedDocument()
    heading = nd.add_text(
        text="5 Инструкция",
        page_from=7,
        section_path="5 Инструкция",
        role="heading",
        origin="docling",
    )
    body = nd.add_text(
        text="1. Авторизоваться в системе.",
        page_from=7,
        section_path="5 Инструкция",
        parent_section_id=heading.element_id,
        origin="docling",
    )
    table = nd.add_structured(
        element_type="table",
        text="| A | B |\n| --- | --- |\n| 1 | 2 |",
        data={"columns": ["A", "B"], "rows": [["1", "2"]]},
        table={"columns": ["A", "B"], "rows": [["1", "2"]]},
        page_from=10,
        section_path="5 Инструкция",
        origin="docling",
    )
    return nd, heading, body, table


def test_resolve_text_with_context_and_table():
    nd, heading, body, table = _nd_with_sources()
    raw = [
        {
            "local_id": "c1",
            "type": "text",
            "title": "5 Инструкция",
            "source_reference_ids": body.source_reference_ids,
            "context_source_reference_ids": heading.source_reference_ids,
        },
        {
            "local_id": "c2",
            "type": "table",
            "title": "Бюджет",
            "source_reference_ids": table.source_reference_ids,
        },
    ]
    chunks = nd.resolve_chunks(
        raw,
        source_name="doc.pdf",
        document_id="doc",
        version_id="sha256-abc",
        source_sha256="abc",
        artifact="original.pdf",
    )
    assert len(chunks) == 2
    assert "5 Инструкция" in chunks[0]["content"]
    assert "Авторизоваться" in chunks[0]["content"]
    assert chunks[0]["page"] == 7
    assert chunks[1]["type"] == "table"
    assert chunks[1]["data"] == {
        "columns": ["A", "B"],
        "rows": [["1", "2"]],
    }
    assert chunks[1]["page"] == 10


def test_table_row_selector_keeps_header():
    nd = norm.NormalizedDocument()
    table = nd.add_structured(
        element_type="table",
        text="| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |",
        data={
            "columns": ["A", "B"],
            "rows": [["1", "2"], ["3", "4"]],
        },
        table={
            "columns": ["A", "B"],
            "rows": [["1", "2"], ["3", "4"]],
        },
        page_from=10,
        section_path="5 Инструкция",
        origin="docling",
    )
    raw = [
        {
            "type": "table",
            "source_reference_ids": table.source_reference_ids,
            "selectors": [
                {
                    "source_reference_id": table.source_reference_ids[0],
                    "kind": "table_rows",
                    "start": 2,
                    "end_exclusive": 3,
                    "include_header_rows": [0],
                }
            ],
        }
    ]
    chunks = nd.resolve_chunks(
        raw,
        source_name="doc.pdf",
        document_id="doc",
        version_id="sha256-abc",
        source_sha256="abc",
        artifact="original.pdf",
    )
    assert len(chunks) == 1
    assert "| A | B |" in chunks[0]["content"]
    assert "| 3 | 4 |" in chunks[0]["content"]
    assert "| 1 | 2 |" not in chunks[0]["content"]


def test_deterministic_fallback_groups_text_and_keeps_tables():
    nd, _heading, _body, _table = _nd_with_sources()
    chunks = nd.fallback_chunks(
        source_name="doc.pdf",
        document_id="doc",
        version_id="sha256-abc",
        source_sha256="abc",
        artifact="original.pdf",
    )
    assert len(chunks) == 2
    assert "5 Инструкция" in chunks[0]["content"]
    assert "Авторизоваться" in chunks[0]["content"]
    assert chunks[1]["type"] == "table"


def test_new_prompts_receive_normalized_contract():
    nd, _heading, _body, _table = _nd_with_sources()
    chunk_prompt = vp.normalized_chunker_prompt.render(
        document_id="doc",
        version_id="sha256-abc",
        target_tokens=600,
        normalized_elements=nd.prompt_elements(),
    )
    assert 'document_id: "doc"' in chunk_prompt
    assert '"element_id": "e1"' in chunk_prompt
    assert "normalized elements:\n[]" not in chunk_prompt

    analysis_prompt = vp.normalized_analysis_prompt.render(
        region_id="doc:4:1",
        task="extract",
        mode="region",
        candidate_elements=[
            {"element_id": "e1", "type": "text", "text": "heading"}
        ],
        page_text="page text",
        analysis="",
        retry_note="",
    )
    assert 'region_id: "doc:4:1"' in analysis_prompt
    assert '"element_id": "e1"' in analysis_prompt
    assert '"page text"' in analysis_prompt


def test_finalize_merges_toc_siblings_and_reindexes():
    chunks = [
        {
            "type": "text",
            "page": 2,
            "title": "Table of Contents",
            "content": "Table of Contents\n\n1 Содержание ......... 3",
            "data": None,
            "text": "",
            "id": "old-1",
            "chunk_index": 0,
            "_order": 1,
        },
        {
            "type": "text",
            "page": 2,
            "title": "Table of Contents",
            "content": "Table of Contents\n\n2 Схема ......... 4",
            "data": None,
            "text": "",
            "id": "old-2",
            "chunk_index": 1,
            "_order": 2,
        },
    ]
    result = norm.finalize_chunks(
        chunks, document_id="doc", version_id="sha256-abc"
    )
    assert len(result) == 1
    assert "1 Содержание" in result[0]["content"]
    assert "2 Схема" in result[0]["content"]
    assert result[0]["chunk_index"] == 0
    assert result[0]["id"] == "doc:sha256-abc:p2:c0"
    assert "_order" not in result[0]
