"""openparse segmentation + the same two-stage Jinja2 VLM prompts as per_page.py."""

import json
import re
from pathlib import Path

import openparse
import pymupdf
from jinja2 import Environment, FileSystemLoader
from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR.parent / "prompts"
OUTPUT_DIR = BASE_DIR

PDF_PATH = BASE_DIR.parent / "data/knowledge/БП Командировка.pdf"
DPI = 150
MAX_TOKENS = 2048
CROP_PADDING = 4  # px around a node bbox so glyph edges are not clipped
IMAGE_AREA_RATIO = 0.03  # embedded image must cover this share of the page
NODE_IMAGE_OVERLAP_RATIO = 0.6  # skip an image already covered by a node
MIN_PAGE_TEXT_CHARS = 40  # text on an otherwise image-only page worth keeping

#MLX-VLM
model_path = "mlx-community/gemma-4-e4b-it-4bit"
model, processor = load(model_path)

env = Environment(loader=FileSystemLoader(PROMPTS_DIR))
analysis_prompt = env.get_template("image_analysis.j2")
markdown_prompt = env.get_template("markdown_prompt.j2")

parser = openparse.DocumentParser()

# PNGs generated during the run; removed when parsing finishes or fails.
created_pngs = []


def run_vision(prompt, images):
    formatted_prompt = apply_chat_template(
        processor, model.config, prompt, num_images=len(images)
    )
    output = generate(
        model=model,
        processor=processor,
        prompt=formatted_prompt,
        image=[str(image) for image in images],
        max_tokens=MAX_TOKENS,
        temperature=0.0,
    )
    return getattr(output, "text", None) or str(output)


def parse_analysis(raw):
    """Pull the JSON object out of the model's analysis response."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def render_region(doc, page_index, clip, filename):
    """Save one page region as a PNG and remember it for cleanup."""
    page = doc.load_page(page_index)
    image_path = OUTPUT_DIR / filename
    page.get_pixmap(dpi=DPI, clip=clip & page.rect).save(image_path)
    created_pngs.append(image_path)
    return image_path


def render_node(doc, node, node_index):
    """Render every page region a node covers, in reading order."""
    images = []
    for bbox in node.bbox:
        page = doc.load_page(bbox.page)
        # openparse uses bottom-left coordinates, PyMuPDF uses top-left.
        clip = pymupdf.Rect(
            bbox.x0 - CROP_PADDING,
            bbox.page_height - bbox.y1 - CROP_PADDING,
            bbox.x1 + CROP_PADDING,
            bbox.page_height - bbox.y0 + CROP_PADDING,
        )
        images.append(
            render_region(
                doc, bbox.page, clip, f"node_{node_index:02d}_page_{bbox.page + 1}.png"
            )
        )
    return images


def render_page(doc, page_index):
    """Render a whole page, used when openparse and images give nothing."""
    page = doc.load_page(page_index)
    return [
        render_region(doc, page_index, page.rect, f"page_{page_index + 1}.png")
    ]


def render_page_text(page_index, image_rects):
    """Render a page with large images whited out, keeping its text."""
    doc = pymupdf.open(PDF_PATH)
    page = doc.load_page(page_index)
    for rect in image_rects:
        page.add_redact_annot(rect)
    page.apply_redactions()

    image_path = OUTPUT_DIR / f"text_page_{page_index + 1}.png"
    page.get_pixmap(dpi=DPI).save(image_path)
    created_pngs.append(image_path)
    doc.close()
    return [image_path]


def render_embedded_image(page_index, xref, image_index):
    """Save an embedded image at its native resolution."""
    doc = pymupdf.open(PDF_PATH)
    pix = pymupdf.Pixmap(doc, xref)
    if pix.n - pix.alpha >= 4:  # CMYK or other device space
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    elif pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

    image_path = OUTPUT_DIR / f"image_{page_index + 1}_{image_index:02d}.png"
    pix.save(image_path)
    created_pngs.append(image_path)
    doc.close()
    return image_path


def bbox_to_rect(bbox):
    """Convert an openparse (bottom-left) bbox to a PyMuPDF (top-left) rect."""
    return pymupdf.Rect(
        bbox.x0,
        bbox.page_height - bbox.y1,
        bbox.x1,
        bbox.page_height - bbox.y0,
    )


def overlap_ratio(rect, other):
    """Share of `rect` covered by `other`."""
    area = rect.get_area()
    if area <= 0:
        return 0.0
    return abs(rect & other).get_area() / area


def find_content_images(page, node_rects):
    """Return large embedded images that openparse did not already cover."""
    images = []
    page_area = page.rect.get_area()

    for info in page.get_image_info(xrefs=True):
        if not info.get("xref"):
            continue
        rect = pymupdf.Rect(info["bbox"])
        if rect.get_area() < IMAGE_AREA_RATIO * page_area:
            continue
        if any(overlap_ratio(rect, node_rect) >= NODE_IMAGE_OVERLAP_RATIO
               for node_rect in node_rects):
            continue
        images.append({"xref": info["xref"], "rect": rect})

    return images


def build_units(parsed_doc, doc):
    """Merge parsed nodes, embedded images and fallback page units.

    openparse is text/table based: a diagram or screenshot stored as a raster
    image produces no nodes. Embedded images are therefore extracted at native
    resolution, and pages with neither nodes nor images are rendered whole.
    Units are sorted in reading order: page text, parsed nodes, then images.
    """
    units = []
    covered_pages = set()
    node_rects_by_page = {}

    for node_index, node in enumerate(parsed_doc.nodes):
        pages = sorted({bbox.page for bbox in node.bbox})
        if not pages:
            continue
        covered_pages.update(pages)
        for bbox in node.bbox:
            node_rects_by_page.setdefault(bbox.page, []).append(bbox_to_rect(bbox))
        units.append(
            {
                "kind": "node",
                "order": 1,
                "index": node_index,
                "node": node,
                "pages": pages,
            }
        )

    for page_index in range(doc.page_count):
        page = doc.load_page(page_index)
        images = find_content_images(page, node_rects_by_page.get(page_index, []))

        if images:
            page_text = " ".join(page.get_text().split())
            if page_index not in covered_pages and len(page_text) >= MIN_PAGE_TEXT_CHARS:
                units.append(
                    {
                        "kind": "page-text",
                        "order": 0,
                        "index": page_index,
                        "rects": [image["rect"] for image in images],
                        "pages": [page_index],
                    }
                )
            for image_index, image in enumerate(images):
                units.append(
                    {
                        "kind": "image",
                        "order": 2,
                        "index": page_index,
                        "image_index": image_index,
                        "image": image,
                        "pages": [page_index],
                    }
                )
        elif page_index not in covered_pages:
            units.append(
                {
                    "kind": "page",
                    "order": 3,
                    "index": page_index,
                    "pages": [page_index],
                }
            )

    units.sort(key=lambda unit: (unit["pages"][0], unit["order"]))
    return units


def extract_markdown(images):
    """Run the analysis + markdown VLM passes over a group of pictures."""
    analysis = parse_analysis(run_vision(analysis_prompt.render(), images))

    return run_vision(
        markdown_prompt.render(
            extracted_text=analysis.get("extracted_text", ""),
            confidence_score_text=analysis.get("confidence_score_text", 0),
            tables_detected=analysis.get("tables_detected", "No"),
            latex_equations_detected=analysis.get("latex_equations_detected", "No"),
            diagram_detected=analysis.get("diagram_detected", "No"),
            diagram_type=analysis.get("diagram_type", ""),
            diagram_description=analysis.get("diagram_description", ""),
            diagram_nodes=analysis.get("diagram_nodes", []),
            diagram_connections=analysis.get("diagram_connections", []),
        ),
        images,
    )


def parse_pdf():
    parsed_doc = parser.parse(str(PDF_PATH))
    render_doc = pymupdf.open(PDF_PATH)
    sections = []

    for unit in build_units(parsed_doc, render_doc):
        pages = unit["pages"]

        if unit["kind"] == "node":
            images = render_node(render_doc, unit["node"], unit["index"])
            label = f"node {unit['index']} (page {', '.join(str(p + 1) for p in pages)})"
        elif unit["kind"] == "page-text":
            images = render_page_text(unit["index"], unit["rects"])
            label = f"page {pages[0] + 1} (text around images)"
        elif unit["kind"] == "image":
            images = [
                render_embedded_image(
                    unit["index"], unit["image"]["xref"], unit["image_index"]
                )
            ]
            label = f"page {pages[0] + 1} (embedded image {unit['image_index'] + 1})"
        else:
            images = render_page(render_doc, unit["index"])
            label = f"page {pages[0] + 1} (full page, no parsed nodes)"

        if not images:
            continue

        markdown = extract_markdown(images)
        print(f"{label}: {len(markdown)} chars")
        sections.append(f"<!-- {label} -->\n\n{markdown.strip()}")

    render_doc.close()

    md_path = OUTPUT_DIR / f"{PDF_PATH.stem}.md"
    md_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")

    print(f"Saved parsed output to {md_path}")


def main():
    try:
        parse_pdf()
    finally:
        for image_path in created_pngs:
            image_path.unlink(missing_ok=True)
        created_pngs.clear()


if __name__ == "__main__":
    main()
