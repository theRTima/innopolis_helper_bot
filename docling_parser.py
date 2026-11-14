"""Docling-first document ingestion for the Innopolis helper bot.

Docling reads the document the normal way: text, lists, headings and tables.
The vision model is only used where Docling cannot produce usable content:

* pictures and diagrams that carry process logic (``PictureItem``), which are
  extracted from the PDF at native resolution;
* pages with no readable text layer at all (scans, screenshot-only pages).

Text and tables are chunked with Docling's ``HybridChunker``. Tables are taken
out of the chunker input and stored as one markdown chunk each, because the
default table serialisation loses the row/column structure.

The output is a JSONL file of self-contained RAG chunks plus a Markdown preview.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pymupdf
from docling.chunking import HybridChunker
from docling.document_converter import DocumentConverter

import vision_parser as vp

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PDF = vp.DEFAULT_PDF

# A page with less readable text than this is treated as image-only and sent to
# the vision model as a whole page.
MIN_PAGE_TEXT = 30

# Two Docling picture items that cover (almost) the same area are the same
# embedded image and must not be parsed twice.
PICTURE_IOU_DEDUP = 0.8


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


def item_order_map(doc):
    """Reading-order index for every item in the document."""
    return {
        item.self_ref: index
        for index, (item, _level) in enumerate(doc.iterate_items())
    }


def heading_map(doc):
    """Nearest heading path for every item in the document."""
    result = {}
    stack = {}
    for item, _level in doc.iterate_items():
        if str(getattr(item, "label", "")) == "section_header":
            level = getattr(item, "level", 1) or 1
            stack = {key: value for key, value in stack.items() if key < level}
            stack[level] = (item.text or "").strip()
        if stack:
            result[item.self_ref] = " > ".join(
                stack[key] for key in sorted(stack) if stack[key]
            )
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


def first_page(items):
    """Page number shared by a chunk's items (first item wins)."""
    for item in items:
        page = iter_prov_page(item)
        if page is not None:
            return page
    return None


def chunk_headings(chunk, chunks_headings, item_ref):
    """Heading path for a Docling chunk."""
    if chunk.meta.headings:
        return " > ".join(chunk.meta.headings)
    return chunks_headings.get(item_ref, "")


def make_chunk(pdf_name, page, title, content, chunk_type, data, origin, order):
    return {
        "id": "",
        "source": pdf_name,
        "page": page,
        "chunk_index": 0,
        "origin": origin,
        "type": chunk_type,
        "title": title.strip(),
        "content": content.strip(),
        "data": data,
        "text": vp.build_search_text(title, content, data),
        "_order": order,
    }


def table_chunk(pdf_name, doc, table, headings, order):
    """One structured chunk per table, with a clean markdown rendering."""
    page = iter_prov_page(table) or 1
    context = headings.get(table.self_ref, "")
    title = context.split(" > ")[-1] if context else ""

    markdown = table.export_to_markdown(doc).strip()
    content = f"{context}\n\n{markdown}" if context else markdown

    grid = table.data.grid
    columns = [cell.text.strip() for cell in grid[0]] if grid else []
    rows = (
        [[cell.text.strip() for cell in row] for row in grid[1:]]
        if len(grid) > 1
        else []
    )
    data = {"columns": columns, "rows": rows}
    return make_chunk(
        pdf_name, page, title, content, "table", data, "docling", order
    )


def text_chunks(pdf_name, doc, selected_pages, orders, headings):
    """Text/list chunks from Docling's hybrid chunker, without tables."""
    document = doc.model_copy(deep=True)
    document.delete_items(node_items=document.tables)

    chunker = HybridChunker()
    chunks = []
    for chunk in chunker.chunk(dl_doc=document):
        items = chunk.meta.doc_items
        if not items:
            continue
        if all(is_furniture(item) for item in items):
            continue
        if any(str(getattr(item, "label", "")) == "table" for item in items):
            # Tables are emitted separately with their row/column structure.
            continue

        page = first_page(items)
        if page is None or page not in selected_pages:
            continue

        content = vp.clean_content(chunk.text)
        if not content:
            continue

        context = chunk_headings(chunk, headings, items[0].self_ref)
        title = context.split(" > ")[-1] if context else ""
        if context and context not in content:
            content = f"{context}\n\n{content}"

        order = min(orders.get(item.self_ref, 10**9) for item in items)
        chunks.append(
            make_chunk(
                pdf_name, page, title, content, "text", None, "docling", order
            )
        )
    return chunks


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


def picture_chunks(
    pdf_path,
    doc,
    pdf_doc,
    selected_pages,
    orders,
    headings,
    out_dir,
    created_images,
):
    """Vision chunks for every picture on the selected pages."""
    chunks = []
    seen_rects = {}

    for picture_index, picture in enumerate(doc.pictures):
        page_no = iter_prov_page(picture)
        if page_no is None or page_no not in selected_pages or not picture.prov:
            continue

        pdf_page = pdf_doc.load_page(page_no - 1)
        target = bbox_to_rect(pdf_page, picture.prov[0].bbox)

        # One embedded image can produce several Docling picture items.
        previous = seen_rects.get(page_no)
        if previous is not None:
            intersection = previous & target
            union = previous | target
            if not intersection.is_empty and (
                intersection.get_area() / max(union.get_area(), 1.0)
                > PICTURE_IOU_DEDUP
            ):
                continue
        seen_rects[page_no] = target

        match = matching_embedded_image(pdf_page, target)
        if match is not None:
            image_path = vp.render_embedded_image(
                pdf_doc, match[1], page_no - 1, picture_index, out_dir
            )
        else:
            image_path = render_region(pdf_page, target, out_dir, picture_index)
        created_images.append(image_path)

        print(f"page {page_no}: picture {picture_index + 1}")
        vision_chunks = vp.extract_chunks(
            image_path,
            pdf_path.name,
            page_no,
            "image",
            start_index=0,
        )

        context = headings.get(picture.self_ref, "")
        order = orders.get(picture.self_ref, 10**9)
        for chunk in vision_chunks:
            title = chunk["title"]
            content = chunk["content"]
            if context and context not in content:
                content = f"{context}\n\n{content}"
                if not title:
                    title = context.split(" > ")[-1]
            chunk.update(
                {
                    "title": title,
                    "content": content,
                    "origin": "vision",
                    "text": vp.build_search_text(title, content, chunk["data"]),
                    "_order": order,
                }
            )
            chunks.append(chunk)
    return chunks


def full_page_vision_chunks(
    pdf_path, pdf_doc, page_no, out_dir, created_images, start_order
):
    """Vision fallback for a page where Docling found no readable text."""
    page = pdf_doc.load_page(page_no - 1)
    image_path = out_dir / f"page_{page_no:03d}_vision.png"
    page.get_pixmap(dpi=vp.DPI).save(image_path)
    created_images.append(image_path)

    print(f"page {page_no}: vision fallback")
    chunks = vp.extract_chunks(
        image_path,
        pdf_path.name,
        page_no,
        "page",
        start_index=0,
        page_text=page.get_text("text").strip(),
    )
    for offset, chunk in enumerate(chunks):
        chunk["origin"] = "vision_page"
        chunk["_order"] = start_order + offset
    return chunks


def finalize_page(chunks, pdf_name, page_no):
    """Deduplicate, clean and re-index all chunks of one page."""
    unique = []
    seen = set()
    for chunk in chunks:
        key = (
            chunk["type"],
            chunk["content"],
            json.dumps(chunk["data"], sort_keys=True, ensure_ascii=False),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(chunk)

    finalized = vp.postprocess_page_chunks(unique, pdf_name, page_no)
    for chunk in finalized:
        chunk.pop("_order", None)
    return finalized


def parse_document(pdf_path, out_dir=None, pages_spec=None):
    """Parse a PDF into Docling + vision RAG chunks."""
    pdf_path = Path(pdf_path).expanduser().resolve()
    out_dir = Path(out_dir).expanduser().resolve() if out_dir else BASE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_doc = pymupdf.open(pdf_path)
    page_count = pdf_doc.page_count
    selected = set(page + 1 for page in vp.parse_pages(pages_spec, page_count))
    if not selected:
        raise SystemExit("No pages selected.")

    first, last = min(selected), max(selected)
    print(f"Docling: pages {first}-{last}")
    converter = DocumentConverter()
    result = converter.convert(pdf_path, page_range=(first, last))
    doc = result.document

    orders = item_order_map(doc)
    headings = heading_map(doc)
    skip_refs = picture_child_refs(doc)
    body_chars = page_body_chars(doc, skip_refs)
    pictures_by_page = {}
    for picture in doc.pictures:
        page = iter_prov_page(picture)
        if page is not None:
            pictures_by_page[page] = pictures_by_page.get(page, 0) + 1
    tables_by_page = {}
    for table in doc.tables:
        page = iter_prov_page(table)
        if page is not None:
            tables_by_page[page] = tables_by_page.get(page, 0) + 1

    chunks = text_chunks(pdf_path.name, doc, selected, orders, headings)
    chunks.extend(
        table_chunk(pdf_path.name, doc, table, headings, orders.get(table.self_ref, 0))
        for table in doc.tables
        if iter_prov_page(table) in selected
    )

    created_images = []
    try:
        chunks.extend(
            picture_chunks(
                pdf_path,
                doc,
                pdf_doc,
                selected,
                orders,
                headings,
                out_dir,
                created_images,
            )
        )

        # Pages that Docling could not read and that have no picture to parse.
        fallback_pages = [
            page
            for page in selected
            if body_chars.get(page, 0) < MIN_PAGE_TEXT
            and not pictures_by_page.get(page)
            and not tables_by_page.get(page)
        ]
        for page in fallback_pages:
            chunks = [
                chunk for chunk in chunks if chunk["page"] != page
            ]
            chunks.extend(
                full_page_vision_chunks(
                    pdf_path,
                    pdf_doc,
                    page,
                    out_dir,
                    created_images,
                    start_order=10**8,
                )
            )
    finally:
        pdf_doc.close()
        for image_path in created_images:
            image_path.unlink(missing_ok=True)

    final = []
    for page in sorted(selected):
        page_chunks = sorted(
            (chunk for chunk in chunks if chunk["page"] == page),
            key=lambda chunk: chunk.get("_order", 10**9),
        )
        final.extend(finalize_page(page_chunks, pdf_path.name, page))

    jsonl_path, md_path = vp.write_outputs(final, pdf_path, out_dir)
    print(f"Saved {len(final)} chunks to {jsonl_path}")
    print(f"Human-readable preview: {md_path}")
    return final


def main():
    parser = argparse.ArgumentParser(
        description="Parse a PDF with Docling, using vision for pictures and unreadable pages."
    )
    parser.add_argument("pdf", nargs="?", default=str(DEFAULT_PDF), help="PDF path")
    parser.add_argument(
        "--out-dir", default=None, help="Output directory (default: repo root)"
    )
    parser.add_argument(
        "--pages",
        default=None,
        help="1-based pages to parse, e.g. '4' or '4,7-9' (default: all)",
    )
    args = parser.parse_args()

    parse_document(args.pdf, out_dir=args.out_dir, pages_spec=args.pages)


if __name__ == "__main__":
    main()
