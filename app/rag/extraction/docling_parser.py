"""Shared Docling helpers for the versioned extraction pipeline.

This module does not produce chunks by itself. It provides page/item helpers,
table validation and picture-region extraction used by
``app.rag.extraction.new_pipeline``.
"""

from __future__ import annotations

import re

import pymupdf

from app.rag.extraction import vision_parser as vp

# A page with less readable text than this is treated as image-only and sent to
# the vision model as a whole page.
MIN_PAGE_TEXT = 30

# Two Docling picture items that cover almost the same area are the same
# embedded image and must not be parsed twice.
PICTURE_IOU_DEDUP = 0.8

# Docling sometimes classifies a numbered section heading as a list item
# ("5 Инструкция"). This pattern recovers it as page context.
HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\s+[А-ЯЁA-Z]")

# A cell longer than this usually means Docling mistook a paragraph or a
# two-column layout for a table.
MAX_TABLE_CELL_CHARS = 300


def iter_prov_page(item):
    """Page number of a Docling item, or ``None``."""
    prov = getattr(item, "prov", None)
    if not prov:
        return None
    return prov[0].page_no


def is_furniture(item):
    return "FURNITURE" in str(getattr(item, "content_layer", ""))


def picture_child_refs(doc):
    """Text items produced by OCR inside pictures."""
    return {
        child.cref
        for picture in doc.pictures
        for child in getattr(picture, "children", [])
    }


def page_heading_context(doc, skip_refs):
    """Section context and origin page for each page."""
    first_item = {}
    for item, _level in doc.iterate_items():
        page = iter_prov_page(item)
        if page is None or page in first_item:
            continue
        if item.self_ref in skip_refs or is_furniture(item):
            continue
        first_item[page] = item

    result = {}
    current = ""
    current_page = 0
    for page in sorted(first_item):
        item = first_item[page]
        text = (getattr(item, "text", "") or "").strip()
        is_heading = (
            str(getattr(item, "label", "")) == "section_header"
            or (HEADING_RE.match(text) and len(text) <= 80)
        )
        if is_heading:
            current = text
            current_page = page
        result[page] = (current, current_page)
    return result


def page_body_chars(doc, skip_refs):
    """Readable body text per page, ignoring furniture and picture OCR."""
    counts = {}
    for item in doc.texts:
        if item.self_ref in skip_refs or is_furniture(item):
            continue
        page = iter_prov_page(item)
        if page is None:
            continue
        text = vp.clean_content(item.text or "")
        counts[page] = counts.get(page, 0) + len(text)
    return counts


def structured_table(table):
    """Whether Docling found a real row/column table, not a text layout."""
    grid = table.data.grid
    if len(grid) < 2 or len(grid[0]) < 2:
        return False
    longest = max(
        (len(cell.text.strip()) for row in grid for cell in row),
        default=0,
    )
    return longest <= MAX_TABLE_CELL_CHARS


def bbox_to_rect(pdf_page, bbox):
    """Convert a Docling bbox (usually bottom-left) to a PyMuPDF rect."""
    height = pdf_page.rect.height
    if "BOTTOMLEFT" in str(bbox.coord_origin):
        return pymupdf.Rect(bbox.l, height - bbox.t, bbox.r, height - bbox.b)
    return pymupdf.Rect(bbox.l, bbox.t, bbox.r, bbox.b)


def matching_embedded_image(page, target):
    """Find the embedded image that covers the Docling picture bbox."""
    best = None
    for info in page.get_image_info(xrefs=True):
        if not info.get("xref"):
            continue
        rect = pymupdf.Rect(info["bbox"])
        intersection = rect & target
        if intersection.is_empty:
            continue
        coverage = intersection.get_area() / max(target.get_area(), 1.0)
        if coverage < 0.5:
            continue
        if best is None or coverage > best[0]:
            best = (coverage, info["xref"], rect)
    return best


def render_region(page, rect, out_dir, index):
    path = out_dir / f"page_{page.number + 1:03d}_picture_{index:02d}.png"
    page.get_pixmap(clip=rect, dpi=vp.DPI).save(path)
    return path
