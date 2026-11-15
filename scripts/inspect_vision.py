"""Inspect one embedded PDF image with the configured local vision model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingestion import vision_parser as vision


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", type=int, required=True, help="1-based PDF page")
    parser.add_argument("--image", type=int, default=0, help="0-based image on page")
    args = parser.parse_args()

    with pymupdf.open(args.pdf) as document:
        if not 1 <= args.page <= document.page_count:
            parser.error(f"Page must be between 1 and {document.page_count}")
        page = document[args.page - 1]
        images = vision.find_content_images(page)
        if not 0 <= args.image < len(images):
            parser.error(f"Page {args.page} has {len(images)} content images")
        image = images[args.image]
        print("PDF text on page:\n" + page.get_text("text").strip()[:2000], flush=True)
        print(f"Image {args.image}: area={image['rect'].get_area():.0f}", flush=True)
        with TemporaryDirectory() as temp_dir:
            path = vision.render_embedded_image(
                document, image["xref"], args.page - 1, args.image, Path(temp_dir)
            )
            chunks = vision.extract_chunks(path, args.pdf.name, args.page, "image", 0)
        print(json.dumps(chunks, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
