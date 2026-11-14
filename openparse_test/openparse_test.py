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

PDF_PATH = Path("/Users/timur/Downloads/БП Командировка.pdf")
NUM_PAGES = 3
DPI = 150
MAX_TOKENS = 2048
CROP_PADDING = 4  # px around a node bbox so glyph edges are not clipped

#MLX-VLM
model_path = "mlx-community/gemma-4-e4b-it-4bit"
model, processor = load(model_path)

env = Environment(loader=FileSystemLoader(PROMPTS_DIR))
analysis_prompt = env.get_template("image_analysis.j2")
markdown_prompt = env.get_template("markdown_prompt.j2")

parser = openparse.DocumentParser()


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
        ) & page.rect
        image_path = OUTPUT_DIR / f"node_{node_index:02d}_page_{bbox.page + 1}.png"
        page.get_pixmap(dpi=DPI, clip=clip).save(image_path)
        images.append(image_path)
    return images


def main():
    doc = pymupdf.open(PDF_PATH)
    pages = min(NUM_PAGES, doc.page_count)

    for page_index in range(pages):
        page = doc.load_page(page_index)
        page.get_pixmap(dpi=DPI).save(OUTPUT_DIR / f"page_{page_index + 1}.png")

    # Keep only the pages we compare so openparse does not parse the whole document.
    doc.select(range(pages))
    cropped_path = OUTPUT_DIR / PDF_PATH.name
    doc.save(cropped_path)
    doc.close()

    parsed_doc = parser.parse(str(cropped_path))

    render_doc = pymupdf.open(cropped_path)
    sections = []

    for node_index, node in enumerate(parsed_doc.nodes):
        images = render_node(render_doc, node, node_index)
        if not images:
            continue

        analysis = parse_analysis(run_vision(analysis_prompt.render(), images))

        markdown = run_vision(
            markdown_prompt.render(
                extracted_text=analysis.get("extracted_text", ""),
                confidence_score_text=analysis.get("confidence_score_text", 0),
                tables_detected=analysis.get("tables_detected", "No"),
                latex_equations_detected=analysis.get(
                    "latex_equations_detected", "No"
                ),
            ),
            images,
        )

        node_pages = ", ".join(str(bbox.page + 1) for bbox in node.bbox)
        print(f"Node {node_index} (page {node_pages}): {len(markdown)} chars")
        sections.append(
            f"<!-- node {node_index} (page {node_pages}) -->\n\n{markdown.strip()}"
        )

    md_path = OUTPUT_DIR / f"{PDF_PATH.stem}.md"
    md_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")

    print(f"Saved parsed output to {md_path}")


if __name__ == "__main__":
    main()
