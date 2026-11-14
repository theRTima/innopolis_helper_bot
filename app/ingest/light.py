"""PyMuPDF fallback: text pages plus vision for images and scans."""

from __future__ import annotations

from pathlib import Path

import pymupdf

import vision_parser as vision

MIN_PAGE_TEXT = 30


def parse_document(pdf_path: Path, out_dir: Path) -> list[dict]:
    """Build the branch's chunk format without Docling's heavy dependencies."""
    pdf_path = pdf_path.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict] = []

    with pymupdf.open(pdf_path) as document:
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            page_number = page_index + 1
            text = vision.clean_content(page.get_text("text").strip())
            page_chunks: list[dict] = []
            if text:
                page_chunks.extend(
                    vision.normalize_chunks(
                        [{"type": "text", "title": "", "content": text, "data": None}],
                        pdf_path.name,
                        page_number,
                        "text",
                        0,
                    )
                )

            images = vision.find_content_images(page)
            for image_index, image in enumerate(images):
                try:
                    image_path = vision.render_embedded_image(
                        document, image["xref"], page_index, image_index, out_dir
                    )
                except Exception:
                    image_path = out_dir / f"page_{page_number:03d}_image_{image_index:02d}.png"
                    page.get_pixmap(clip=image["rect"], dpi=vision.DPI).save(image_path)
                try:
                    page_chunks.extend(
                        vision.extract_chunks(
                            image_path, pdf_path.name, page_number, "image", len(page_chunks)
                        )
                    )
                finally:
                    image_path.unlink(missing_ok=True)

            if len(text) < MIN_PAGE_TEXT and not images:
                image_path = out_dir / f"page_{page_number:03d}_scan.png"
                page.get_pixmap(dpi=vision.DPI).save(image_path)
                try:
                    page_chunks = vision.extract_chunks(
                        image_path, pdf_path.name, page_number, "page", 0, page_text=text
                    )
                finally:
                    image_path.unlink(missing_ok=True)

            chunks.extend(vision.postprocess_page_chunks(page_chunks, pdf_path.name, page_number))

    vision.write_outputs(chunks, pdf_path, out_dir)
    return chunks
