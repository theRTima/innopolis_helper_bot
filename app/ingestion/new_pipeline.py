"""Extraction pipeline for the versioned RAG store.

This module implements the stage the new prompts were written for:

1. Docling extracts text, tables and picture regions.
2. Picture regions and unreadable pages are analysed with
   ``prompts/image_analysis.j2``.
3. Analysis items become normalized elements with stable source references.
4. ``prompts/vision_chunker.j2`` groups those references into retrieval chunks.
5. ``app.ingestion.normalized`` resolves references back to real text and data.

The model never copies source text into the final chunk.  It only returns
references, so a malformed or incomplete model answer cannot silently rewrite
document facts.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from pathlib import Path

import pymupdf
from docling.document_converter import DocumentConverter

from app.ingestion import docling_parser as dp
from app.ingestion import normalized as norm
from app.ingestion import vision_parser as vp
from app.ingestion.assets import register_asset

MAX_PROMPT_CHARS = 40_000
MAX_BATCH_ELEMENTS = 30
MAX_CANDIDATES = 25


def run_text(prompt: str) -> str:
    """Run the configured text model for the grouping stage."""
    return vp.run_text(prompt)


def analyze_image(
    image_path: Path,
    *,
    region_id: str,
    mode: str,
    page_text: str = "",
    candidates: list[dict] | None = None,
    retry_note: str = "",
) -> tuple[dict, str]:
    """Run the new region/page analysis prompt against one image."""
    prompt = vp.normalized_analysis_prompt.render(
        region_id=region_id,
        task="extract",
        mode=mode,
        candidate_elements=candidates or [],
        page_text=page_text,
        analysis="",
        retry_note=retry_note,
    )
    raw = vp.run_vision(prompt, image_path)
    analysis = vp.parse_json_object(raw)
    if isinstance(analysis.get("items"), list) and analysis["items"]:
        return analysis, raw
    if analysis.get("status") == "empty":
        return analysis, raw

    # Dense pages occasionally produce truncated JSON. Retry once with a
    # compact-output instruction before falling back to deterministic chunks.
    retry_prompt = prompt + (
        "\n\nIMPORTANT: Return one valid JSON object only. "
        "Keep items concise, omit decorative details, and ensure the JSON "
        "is complete and parseable."
    )
    retry_raw = vp.run_vision(retry_prompt, image_path)
    retry_analysis = vp.parse_json_object(retry_raw)
    if (
        isinstance(retry_analysis.get("items"), list)
        and retry_analysis["items"]
    ):
        return retry_analysis, retry_raw
    return retry_analysis or analysis, retry_raw or raw


def _text_from_transcription(item: dict) -> str:
    parts: list[str] = []
    for entry in item.get("transcription") or []:
        if isinstance(entry, dict):
            text = (entry.get("text") or "").strip()
            if text:
                parts.append(text)
        elif isinstance(entry, str) and entry.strip():
            parts.append(entry.strip())
    return "\n".join(parts)


def _text_from_observations(item: dict) -> str:
    parts: list[str] = []
    for entry in item.get("visual_observations") or []:
        if isinstance(entry, dict):
            parts.append(norm.flatten(entry))
        else:
            parts.append(str(entry))
    return "\n".join(part.strip() for part in parts if part.strip())


def _table_markdown(table: dict | None) -> str:
    if not isinstance(table, dict):
        return ""
    cells = table.get("cells")
    if not isinstance(cells, list):
        return ""
    rows: dict[int, dict[int, str]] = defaultdict(dict)
    width = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        row = cell.get("row")
        column = cell.get("column")
        if not isinstance(row, int) or not isinstance(column, int):
            continue
        rows[row][column] = str(cell.get("text") or "")
        width = max(width, column + 1)
    if not rows:
        return ""
    table_rows: list[list[str]] = []
    for row_index in sorted(rows):
        table_rows.append(
            [rows[row_index].get(column, "") for column in range(width)]
        )
    return norm._rows_to_markdown(table_rows)


def _diagram_text(diagram: dict | None) -> str:
    if not isinstance(diagram, dict):
        return ""
    nodes = diagram.get("nodes") or []
    edges = diagram.get("edges") or []
    node_labels: dict[str, str] = {}
    parts: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        label = node.get("label_verbatim") or node.get("label")
        if node_id and label:
            node_labels[node_id] = str(label)
        if label:
            parts.append(str(label))
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        label = edge.get("label_verbatim") or edge.get("label") or ""
        if source and target:
            source_label = node_labels.get(source, source)
            target_label = node_labels.get(target, target)
            parts.append(
                f"{source_label} -> {target_label}"
                + (f" ({label})" if label else "")
            )
    return "\n".join(parts)


def analysis_item_text(item: dict) -> str:
    """Human-readable representation of one image-analysis item."""
    item_type = str(item.get("type") or "image")
    parts = [
        _text_from_transcription(item),
        _text_from_observations(item),
        str(item.get("interpretation") or "").strip(),
    ]
    if item_type == "table":
        parts.append(_table_markdown(item.get("table")))
    if item_type == "diagram":
        parts.append(_diagram_text(item.get("diagram")))
    if item_type in {"screenshot", "form"} and isinstance(
        item.get("ui"), dict
    ):
        parts.append(norm.flatten(item["ui"]))
    return "\n".join(part.strip() for part in parts if part and part.strip())


def analysis_item_data(item: dict) -> dict | None:
    item_type = str(item.get("type") or "image")
    if item_type == "table" and isinstance(item.get("table"), dict):
        return item["table"]
    if item_type == "diagram" and isinstance(item.get("diagram"), dict):
        return item["diagram"]
    if item_type in {"screenshot", "form"} and isinstance(
        item.get("ui"), dict
    ):
        return item["ui"]
    return None


def _regions(item, page: int) -> list[dict]:
    region = {"page": page}
    prov = getattr(item, "prov", None)
    if prov:
        bbox = prov[0].bbox
        region["bbox"] = [bbox.l, bbox.t, bbox.r, bbox.b]
        region["coord_origin"] = str(bbox.coord_origin)
    return [region]


def _heading_like(item, text: str) -> bool:
    label = str(getattr(item, "label", ""))
    if label == "section_header":
        return True
    if label in {"text", "list_item"} and dp.HEADING_RE.match(text):
        return len(text) <= 100 and not text.rstrip().endswith(
            (".", "!", "?", ";")
        )
    return False


def _add_analysis_items(
    nd: norm.NormalizedDocument,
    analysis: dict,
    *,
    page: int,
    section_path: str,
    parent_section_id: str | None,
    asset_id: str,
    bbox: list[float] | None,
    fallback_text: str = "",
) -> int:
    items = analysis.get("items") if isinstance(analysis, dict) else None
    added = 0
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "image").lower()
            if item_type not in norm.ALLOWED_CHUNK_TYPES:
                item_type = "mixed"
            text = analysis_item_text(item)
            data = analysis_item_data(item)
            if not text and data is None:
                continue
            source_id = nd.add_source(
                kind=item_type,
                text=text,
                data=data,
                page_from=page,
                page_to=page,
                section_path=section_path,
                origin="vision",
                asset_ids=[asset_id],
            )
            enrichment = {
                "enrichment_id": f"enr-{source_id}",
                "source_reference_id": source_id,
                "type": item_type,
                "representation": text[:1000],
                "data": data,
            }
            regions = [{"page": page}]
            if bbox:
                regions[0]["bbox"] = bbox
            nd.add_element(
                norm.NormalizedElement(
                    element_id=nd.next_element_id(),
                    element_type=item_type,
                    text_verbatim=text,
                    parent_section_id=parent_section_id,
                    section_path=section_path,
                    source_reference_ids=[source_id],
                    source_regions=regions,
                    asset_ids=[asset_id],
                    table=data if item_type == "table" else None,
                    diagram=data if item_type == "diagram" else None,
                    ui=data if item_type in {"screenshot", "form"} else None,
                    accepted_enrichments=[enrichment],
                )
            )
            added += 1
    if not added and fallback_text:
        nd.add_text(
            text=fallback_text,
            page_from=page,
            page_to=page,
            section_path=section_path,
            parent_section_id=parent_section_id,
            source_regions=[{"page": page}],
            origin="vision",
        )
        added = 1
    return added


def _image_path_for_picture(
    pdf_doc: pymupdf.Document,
    pdf_page: pymupdf.Page,
    picture,
    page_no: int,
    index: int,
    temp_dir: Path,
    seen_rects: dict[int, pymupdf.Rect],
) -> tuple[Path | None, pymupdf.Rect | None]:
    if not getattr(picture, "prov", None):
        return None, None
    target = dp.bbox_to_rect(pdf_page, picture.prov[0].bbox)
    if target.is_empty or target.get_area() <= 0:
        return None, None

    previous = seen_rects.get(page_no)
    if previous is not None:
        intersection = previous & target
        union = previous | target
        if not intersection.is_empty and (
            intersection.get_area() / max(union.get_area(), 1.0)
            > dp.PICTURE_IOU_DEDUP
        ):
            return None, None
    seen_rects[page_no] = target

    match = dp.matching_embedded_image(pdf_page, target)
    if match is not None:
        try:
            return (
                vp.render_embedded_image(
                    pdf_doc, match[1], page_no - 1, index, temp_dir
                ),
                target,
            )
        except Exception:
            pass
    return dp.render_region(pdf_page, target, temp_dir, index), target


def build_from_pdf(
    pdf_path: Path,
    *,
    document_id: str,
    version_id: str,
    temp_dir: Path,
    pages_spec: str | None = None,
) -> tuple[norm.NormalizedDocument, list[str]]:
    """Build normalized elements from a PDF or a rendered Office PDF."""
    pdf_path = Path(pdf_path).resolve()
    pdf_doc = pymupdf.open(pdf_path)
    warnings: list[str] = []
    try:
        page_count = pdf_doc.page_count
        selected = set(
            page + 1 for page in vp.parse_pages(pages_spec, page_count)
        )
        if not selected:
            raise ValueError("No pages selected.")
        first, last = min(selected), max(selected)
        result = DocumentConverter().convert(
            pdf_path, page_range=(first, last)
        )
    except Exception:
        pdf_doc.close()
        raise

    doc = result.document
    nd = norm.NormalizedDocument()
    skip_refs = dp.picture_child_refs(doc)
    page_context = dp.page_heading_context(doc, skip_refs)
    body_chars = dp.page_body_chars(doc, skip_refs)

    heading_stack: list[tuple[int, str, str]] = []
    pictures: list[dict] = []
    candidates_by_page: dict[int, list[dict]] = defaultdict(list)
    pictures_by_page: dict[int, int] = defaultdict(int)
    tables_by_page: dict[int, int] = defaultdict(int)
    elements_by_page: dict[int, int] = defaultdict(int)

    def section_path() -> str:
        return " > ".join(text for _level, _id, text in heading_stack)

    def parent_id() -> str | None:
        return heading_stack[-1][1] if heading_stack else None

    try:
        for item, _level in doc.iterate_items():
            page = dp.iter_prov_page(item)
            if page is None or page not in selected:
                continue
            if dp.is_furniture(item) or item.self_ref in skip_refs:
                continue
            label = str(getattr(item, "label", ""))
            text = (getattr(item, "text", "") or "").strip()

            if _heading_like(item, text):
                text = vp.clean_content(text)
                if not text:
                    continue
                level = getattr(item, "level", 1) or 1
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                path = " > ".join(
                    [entry[2] for entry in heading_stack] + [text]
                )
                element = nd.add_text(
                    text=text,
                    page_from=page,
                    page_to=page,
                    section_path=path,
                    parent_section_id=parent_id(),
                    source_regions=_regions(item, page),
                    role="heading",
                )
                heading_stack.append((level, element.element_id, text))
                elements_by_page[page] += 1
                continue

            if label == "table":
                tables_by_page[page] += 1
                grid = item.data.grid
                if not dp.structured_table(item):
                    cells = [
                        cell.text.strip()
                        for row in grid
                        for cell in row
                        if cell.text.strip()
                    ]
                    if cells:
                        element = nd.add_text(
                            text="\n".join(cells),
                            page_from=page,
                            page_to=page,
                            section_path=section_path(),
                            parent_section_id=parent_id(),
                            source_regions=_regions(item, page),
                        )
                        candidates_by_page[page].append(
                            {
                                "element_id": element.element_id,
                                "type": "text",
                                "text": element.text_verbatim[:500],
                            }
                        )
                        elements_by_page[page] += 1
                    continue
                markdown = item.export_to_markdown(doc).strip()
                columns = [cell.text.strip() for cell in grid[0]]
                rows = [
                    [cell.text.strip() for cell in row] for row in grid[1:]
                ]
                data = {"columns": columns, "rows": rows}
                nd.add_structured(
                    element_type="table",
                    text=markdown,
                    data=data,
                    table=data,
                    page_from=page,
                    page_to=page,
                    section_path=section_path(),
                    parent_section_id=parent_id(),
                    source_regions=_regions(item, page),
                    origin="docling",
                )
                elements_by_page[page] += 1
                continue

            if label == "picture":
                pictures_by_page[page] += 1
                pictures.append(
                    {
                        "item": item,
                        "page": page,
                        "section_path": section_path(),
                        "parent_id": parent_id(),
                    }
                )
                continue

            if text:
                text = vp.clean_content(text)
                if not text:
                    continue
                element = nd.add_text(
                    text=text,
                    page_from=page,
                    page_to=page,
                    section_path=section_path(),
                    parent_section_id=parent_id(),
                    source_regions=_regions(item, page),
                )
                candidates_by_page[page].append(
                    {
                        "element_id": element.element_id,
                        "type": "text",
                        "text": text[:500],
                    }
                )
                elements_by_page[page] += 1

        seen_rects: dict[int, pymupdf.Rect] = {}
        for index, picture_data in enumerate(pictures):
            page_no = picture_data["page"]
            pdf_page = pdf_doc.load_page(page_no - 1)
            image_path, target = _image_path_for_picture(
                pdf_doc,
                pdf_page,
                picture_data["item"],
                page_no,
                index,
                temp_dir,
                seen_rects,
            )
            if image_path is None:
                continue
            asset_id = f"asset-{page_no}-{index}"
            register_asset(
                nd.assets,
                asset_id,
                image_path,
                temp_dir,
                page_no,
                (
                    [target.x0, target.y0, target.x1, target.y1]
                    if target is not None
                    else None
                ),
            )
            analysis, raw = analyze_image(
                image_path,
                region_id=f"{document_id}:{page_no}:{index}",
                mode="region",
                page_text=pdf_page.get_text("text").strip()[:4000],
                candidates=candidates_by_page.get(page_no, [])[
                    :MAX_CANDIDATES
                ],
            )
            added = _add_analysis_items(
                nd,
                analysis,
                page=page_no,
                section_path=picture_data["section_path"],
                parent_section_id=picture_data["parent_id"],
                asset_id=asset_id,
                bbox=(
                    [target.x0, target.y0, target.x1, target.y1]
                    if target
                    else None
                ),
            )
            if not added:
                warnings.append(
                    f"page {page_no}: picture analysis produced no items; "
                    f"raw={raw[:200]!r}"
                )

        for page in sorted(selected):
            if elements_by_page.get(page):
                continue
            if body_chars.get(page, 0) >= dp.MIN_PAGE_TEXT:
                continue
            if pictures_by_page.get(page) or tables_by_page.get(page):
                continue
            pdf_page = pdf_doc.load_page(page - 1)
            image_path = temp_dir / f"page_{page:03d}_vision.png"
            pdf_page.get_pixmap(dpi=vp.DPI).save(image_path)
            register_asset(
                nd.assets, f"asset-{page}-page", image_path, temp_dir, page
            )
            analysis, raw = analyze_image(
                image_path,
                region_id=f"{document_id}:{page}:page",
                mode="page",
                page_text=pdf_page.get_text("text").strip()[:4000],
                candidates=candidates_by_page.get(page, [])[:MAX_CANDIDATES],
            )
            added = _add_analysis_items(
                nd,
                analysis,
                page=page,
                section_path=page_context.get(page, ("", 0))[0],
                parent_section_id=None,
                asset_id=f"asset-{page}-page",
                bbox=None,
                fallback_text=vp.clean_content(
                    pdf_page.get_text("text").strip()
                ),
            )
            if not added:
                warnings.append(
                    f"page {page}: vision fallback produced no items"
                )
    finally:
        pdf_doc.close()

    return nd, warnings


def batch_elements(
    elements: list[dict],
    *,
    max_chars: int = MAX_PROMPT_CHARS,
    max_elements: int = MAX_BATCH_ELEMENTS,
) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for element in elements:
        encoded = json.dumps(element, ensure_ascii=False)
        if current and (
            len(current) >= max_elements or size + len(encoded) > max_chars
        ):
            batches.append(current)
            current = []
            size = 0
        current.append(element)
        size += len(encoded)
    if current:
        batches.append(current)
    return batches


def _element_page(element: dict) -> int:
    regions = element.get("source_regions")
    if isinstance(regions, list):
        pages = [
            region.get("page")
            for region in regions
            if isinstance(region, dict) and isinstance(region.get("page"), int)
        ]
        if pages:
            return min(pages)
    return 1


def chunk_elements(
    elements: list[dict],
    *,
    document_id: str,
    version_id: str,
    target_tokens: int = 600,
) -> tuple[list[dict], list[str]]:
    """Ask the model to group normalized element references per page."""
    warnings: list[str] = []
    raw_chunks: list[dict] = []
    if not elements:
        return raw_chunks, warnings

    pages: list[list[dict]] = []
    current: list[dict] = []
    current_page: int | None = None
    for element in elements:
        page = _element_page(element)
        if current and page != current_page:
            pages.append(current)
            current = []
        current.append(element)
        current_page = page
    if current:
        pages.append(current)

    for page_elements in pages:
        for batch in batch_elements(page_elements):
            prompt = vp.normalized_chunker_prompt.render(
                document_id=document_id,
                version_id=version_id,
                target_tokens=target_tokens,
                normalized_elements=batch,
            )
            raw = run_text(prompt)
            parsed = vp.parse_json_object(raw)
            chunks = parsed.get("chunks")
            if isinstance(chunks, list):
                raw_chunks.extend(
                    chunk for chunk in chunks if isinstance(chunk, dict)
                )
            else:
                warnings.append("chunker returned no JSON chunks array")
            for excluded in parsed.get("excluded_sources") or []:
                if isinstance(excluded, dict):
                    warnings.append(
                        "chunker excluded source "
                        f"{excluded.get('source_reference_id')}: "
                        f"{excluded.get('reason', '')}"
                    )
            for unresolved in parsed.get("unresolved") or []:
                if isinstance(unresolved, dict):
                    warnings.append(
                        "chunker unresolved "
                        f"{unresolved.get('source_reference_id')}: "
                        f"{unresolved.get('issue', '')}"
                    )
    return raw_chunks, warnings


def build_from_image(
    image_path: Path,
    *,
    document_id: str,
    version_id: str,
    temp_dir: Path,
) -> tuple[norm.NormalizedDocument, list[str]]:
    """Build normalized elements for a standalone image upload."""
    nd = norm.NormalizedDocument()
    warnings: list[str] = []
    saved_image = temp_dir / ("source" + image_path.suffix.lower())
    shutil.copyfile(image_path, saved_image)
    register_asset(nd.assets, "asset-1-0", saved_image, temp_dir, 1)
    analysis, _raw = analyze_image(
        image_path,
        region_id=f"{document_id}:1:image",
        mode="region",
        page_text="",
        candidates=[],
    )
    added = _add_analysis_items(
        nd,
        analysis,
        page=1,
        section_path="",
        parent_section_id=None,
        asset_id="asset-1-0",
        bbox=None,
        fallback_text=image_path.name,
    )
    if not added:
        warnings.append("image analysis produced no items")
    return nd, warnings


def build_from_text(
    text: str,
    *,
    document_id: str,
    version_id: str,
    source_name: str,
    source_sha256: str,
    artifact: str,
    max_chars: int = 2000,
) -> list[dict]:
    """Line-aware chunking for text, Markdown, HTML and code uploads."""
    lines = text.splitlines()
    chunks: list[dict] = []
    buffer: list[str] = []
    size = 0
    start_line = 1

    def flush() -> None:
        nonlocal buffer, size, start_line
        content = "\n".join(buffer).strip()
        if not content:
            buffer = []
            size = 0
            return
        index = len(chunks)
        title = buffer[0].strip()[:100] if buffer else ""
        chunk = {
            "id": f"{document_id}:{version_id}:p1:c{index}",
            "source": source_name,
            "document_id": document_id,
            "version_id": version_id,
            "source_sha256": source_sha256,
            "artifact": artifact,
            "page": 1,
            "page_to": 1,
            "chunk_index": index,
            "origin": "text",
            "type": "text",
            "title": title,
            "content": content,
            "data": None,
            "text": norm.build_search_text(title, content, None),
            "locator": {"kind": "line", "value": start_line},
        }
        chunks.append(chunk)
        buffer = []
        size = 0
        start_line += content.count("\n") + 1

    for line in lines:
        if buffer and size + len(line) + 1 > max_chars:
            flush()
        buffer.append(line)
        size += len(line) + 1
    flush()
    return chunks
