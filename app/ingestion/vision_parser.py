"""Vision-only alternative for turning a PDF into machine-usable RAG chunks.

The vision model decides where one semantic chunk ends and the next begins, and
transcribes all content -- including diagrams, screenshots, forms and
output-file icons -- into chunks for the Telegram assistant.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pymupdf
from jinja2 import Environment, FileSystemLoader
from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(__file__).resolve().parents[2]
PROMPTS_DIR = BASE_DIR / "prompts"

MODEL_ID = "mlx-community/gemma-4-e4b-it-4bit"
DPI = 200
MAX_TOKENS = 4096

# An embedded image is treated as document content if it covers this share of
# the page. Small logos/icons are ignored.
IMAGE_AREA_RATIO = 0.03

BOILERPLATE_RE = re.compile(
    r"Ошибка! Используйте вкладку.*?(?:–|-)\s*\d+", re.DOTALL
)
HEADER_LINE_RE = re.compile(
    r"^(?:ЕИС\s*[–-]\s*БП Командировка|Exported on\b.*|\d+)$"
)
MAX_CHUNK_CHARS = 2000

_model = None
_processor = None

env = Environment(loader=FileSystemLoader(PROMPTS_DIR))
# Legacy image -> chunk flow used by the flat ingestion CLI.
chunker_prompt = env.get_template("legacy/vision_chunker.j2")
analysis_prompt = env.get_template("legacy/image_analysis.j2")
# Normalized-element flow used by rag_pipeline.extract.
normalized_chunker_prompt = env.get_template("vision_chunker.j2")
normalized_analysis_prompt = env.get_template("image_analysis.j2")


def get_model():
    """Load the VLM once, on first use."""
    global _model, _processor
    if _model is None:
        _model, _processor = load(MODEL_ID)
    return _model, _processor


def run_vision(prompt, image_path):
    model, processor = get_model()
    formatted_prompt = apply_chat_template(
        processor, model.config, prompt, num_images=1
    )
    output = generate(
        model=model,
        processor=processor,
        prompt=formatted_prompt,
        image=[str(image_path)],
        max_tokens=MAX_TOKENS,
        temperature=0.0,
    )
    return getattr(output, "text", None) or str(output)


def parse_json_object(raw):
    """Extract the first JSON object from a model response."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", raw):
        try:
            obj, _ = decoder.raw_decode(raw[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def flatten(value):
    """Flatten nested metadata into searchable text."""
    if isinstance(value, dict):
        return " ".join(flatten(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(flatten(item) for item in value)
    if value is None:
        return ""
    return str(value).strip()


def build_search_text(title, content, data):
    """Text that will be embedded into the vector index."""
    parts = [title, content, flatten(data)]
    return "\n".join(part for part in parts if part)


def normalize_chunks(raw_chunks, pdf_name, page_number, mode, start_index):
    """Validate model output and attach retrieval metadata."""
    chunks = []
    if not isinstance(raw_chunks, list):
        return chunks

    for item in raw_chunks:
        if not isinstance(item, dict):
            continue

        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        data = item.get("data")
        if not content and not data:
            continue

        chunk_type = str(item.get("type") or "text").strip().lower()
        index = start_index + len(chunks)
        chunks.append(
            {
                "id": f"{pdf_name}:p{page_number}:c{index}",
                "source": pdf_name,
                "page": page_number,
                "chunk_index": index,
                "origin": mode,
                "type": chunk_type,
                "title": title,
                "content": content,
                "data": data,
                "text": build_search_text(title, content, data),
            }
        )
    return chunks


def coverage_ratio(chunks, reference):
    """How much of the page's text layer the model managed to cover."""
    reference = clean_content(reference)
    if not reference:
        return 1.0
    total = sum(len(clean_content(chunk["content"])) for chunk in chunks)
    return total / len(reference)


def extract_chunks(
    image_path, pdf_name, page_number, mode, start_index, page_text="", retry_note=""
):
    analysis = None
    if mode == "image":
        # Diagram labels and branches are easy to lose in a single pass, so
        # first extract a detailed structured description, then chunk it.
        analysis = parse_json_object(run_vision(analysis_prompt.render(), image_path))

    prompt = chunker_prompt.render(
        mode=mode,
        analysis=(
            json.dumps(analysis, ensure_ascii=False, indent=2) if analysis else ""
        ),
        page_text=page_text,
        retry_note=retry_note,
    )
    raw = run_vision(prompt, image_path)
    parsed = parse_json_object(raw)
    raw_chunks = parsed.get("chunks")
    if raw_chunks is None and ("content" in parsed or "type" in parsed):
        # The model sometimes returns a single chunk without the wrapper.
        raw_chunks = [parsed]
    chunks = normalize_chunks(raw_chunks, pdf_name, page_number, mode, start_index)

    # A valid empty chunks array means the model found no meaningful content.
    if raw_chunks is not None:
        return chunks
    if chunks:
        return chunks

    # Never silently lose a page: keep the raw answer if the model refused JSON.
    return normalize_chunks(
        [{"type": "mixed", "title": "", "content": raw, "data": None}],
        pdf_name,
        page_number,
        mode,
        start_index,
    )


def clean_content(content):
    """Drop running headers/footers and Word template noise."""
    content = BOILERPLATE_RE.sub("", content)
    lines = [
        line
        for line in content.splitlines()
        if not HEADER_LINE_RE.match(line.strip())
    ]
    return "\n".join(lines).strip()


def split_long_chunk(chunk):
    """Split an oversized text chunk at line boundaries for better retrieval."""
    content = chunk["content"]
    if chunk["type"] != "text" or len(content) <= MAX_CHUNK_CHARS:
        return [chunk]

    parts, current, size = [], [], 0
    for line in content.splitlines():
        if current and size + len(line) + 1 > MAX_CHUNK_CHARS:
            parts.append("\n".join(current).strip())
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current).strip())

    return [dict(chunk, content=part) for part in parts if part]


def is_heading_only(chunk):
    """A short text chunk that only names the section it belongs to."""
    if chunk["type"] != "text" or chunk["data"]:
        return False
    content = chunk["content"].strip()
    if not content or len(content) > 100 or len(content.splitlines()) > 2:
        return False
    return not content.endswith((".", "!", "?", ";"))


def postprocess_page_chunks(chunks, pdf_name, page_number):
    """Clean model output and reindex chunks for one page.

    Running headers, export stamps and the artificial Word heading error are
    removed. Heading-only chunks are merged into the following chunk so a
    section title is never stored as a useless standalone chunk.
    """
    cleaned = []
    for chunk in chunks:
        chunk["content"] = clean_content(chunk["content"])
        chunk["title"] = clean_content(chunk["title"])
        if not chunk["content"] and not chunk["data"]:
            continue
        cleaned.append(chunk)

    merged = []
    pending_headings = []
    for chunk in cleaned:
        if is_heading_only(chunk):
            pending_headings.append(chunk)
            continue

        if pending_headings:
            headings = [item["content"].strip() for item in pending_headings]
            chunk["title"] = headings[0]
            chunk["content"] = "\n\n".join([*headings, chunk["content"]]).strip()
            pending_headings = []

        merged.append(chunk)

    # A page that contains only a heading keeps it instead of losing the page.
    merged.extend(pending_headings)

    for chunk in merged:
        if chunk["title"]:
            continue
        first_line = chunk["content"].splitlines()[0].strip() if chunk["content"] else ""
        if first_line and len(first_line) <= 100 and not first_line.endswith((".", "!", "?")):
            chunk["title"] = first_line

    split = []
    for chunk in merged:
        split.extend(split_long_chunk(chunk))

    for index, chunk in enumerate(split):
        chunk["chunk_index"] = index
        chunk["id"] = f"{pdf_name}:p{page_number}:c{index}"
        chunk["text"] = build_search_text(chunk["title"], chunk["content"], chunk["data"])

    return split


def render_text_page(doc, page_index, image_rects, out_dir):
    """Render a page with large embedded images whited out."""
    page = doc.load_page(page_index)
    for rect in image_rects:
        page.add_redact_annot(rect)
    page.apply_redactions()

    path = out_dir / f"page_{page_index + 1:03d}_text.png"
    page.get_pixmap(dpi=DPI).save(path)
    return path


def render_embedded_image(doc, xref, page_index, image_index, out_dir):
    """Save an embedded image at its native resolution."""
    pix = pymupdf.Pixmap(doc, xref)
    if pix.n - pix.alpha >= 4:  # CMYK or another device space
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    elif pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

    path = out_dir / f"page_{page_index + 1:03d}_image_{image_index:02d}.png"
    pix.save(path)
    return path


def find_content_images(page):
    """Return large embedded images that carry document content."""
    images = []
    page_area = page.rect.get_area()

    for info in page.get_image_info(xrefs=True):
        if not info.get("xref"):
            continue
        rect = pymupdf.Rect(info["bbox"])
        if rect.get_area() < IMAGE_AREA_RATIO * page_area:
            continue
        images.append({"xref": info["xref"], "rect": rect})

    return images


def parse_pages(spec, page_count):
    """Parse a 1-based page selection such as ``4`` or ``4,7-9``."""
    if spec is None or not spec.strip():
        return list(range(page_count))

    selected = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"Invalid page selection: {spec!r}")
        if "-" in part:
            bounds = part.split("-")
            if (
                len(bounds) != 2
                or not all(bound.isdecimal() for bound in bounds)
            ):
                raise ValueError(f"Invalid page range: {part!r}")
            start, end = map(int, bounds)
            if start < 1 or end < start or end > page_count:
                raise ValueError(
                    f"Page range {part!r} is outside 1-{page_count} or reversed"
                )
            selected.update(range(start - 1, end))
        else:
            if not part.isdecimal():
                raise ValueError(f"Invalid page number: {part!r}")
            page = int(part)
            if page < 1 or page > page_count:
                raise ValueError(f"Page {page} is outside 1-{page_count}")
            selected.add(page - 1)

    return sorted(selected)


def write_outputs(chunks, pdf_path, out_dir):
    jsonl_path = out_dir / f"{pdf_path.stem}.chunks.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    md_path = out_dir / f"{pdf_path.stem}.chunks.md"
    sections = []
    for chunk in chunks:
        header = f"### [{chunk['type']}] page {chunk['page']}"
        if chunk["title"]:
            header += f" — {chunk['title']}"
        sections.append(f"{header}\n\n{chunk['content']}")
    md_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")

    return jsonl_path, md_path


def parse_document(pdf_path, out_dir=None, pages_spec=None):
    """Parse a PDF into a list of RAG chunks.

    Args:
        pdf_path: PDF to parse.
        out_dir: Directory for temporary images and output files.
        pages_spec: Optional 1-based page selection, e.g. "4" or "4,7-9".
    """
    pdf_path = Path(pdf_path).expanduser().resolve()
    out_dir = (
        Path(out_dir).expanduser().resolve()
        if out_dir
        else PROJECT_DIR / "data" / "processed"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    created_images = []
    doc = pymupdf.open(pdf_path)

    try:
        for page_index in parse_pages(pages_spec, doc.page_count):
            page = doc.load_page(page_index)
            images = find_content_images(page)
            page_text = page.get_text("text").strip()
            page_chunks = []

            text_page = render_text_page(
                doc, page_index, [image["rect"] for image in images], out_dir
            )
            created_images.append(text_page)

            print(f"page {page_index + 1}: text/vector")
            page_chunks = extract_chunks(
                text_page,
                pdf_path.name,
                page_index + 1,
                "page",
                start_index=0,
                page_text=page_text,
            )

            if page_text and coverage_ratio(page_chunks, page_text) < 0.6:
                print(f"page {page_index + 1}: incomplete, retrying")
                retry_chunks = extract_chunks(
                    text_page,
                    pdf_path.name,
                    page_index + 1,
                    "page",
                    start_index=0,
                    page_text=page_text,
                    retry_note=(
                        "Your previous answer covered only part of the page. "
                        "Return ALL sections, tables and numbered instructions "
                        "from this page as separate chunks. Do not stop after "
                        "the first paragraph."
                    ),
                )
                if coverage_ratio(retry_chunks, page_text) > coverage_ratio(
                    page_chunks, page_text
                ):
                    page_chunks = retry_chunks

            # Last resort: keep the clean text layer so no instruction is lost.
            if page_text and coverage_ratio(page_chunks, page_text) < 0.4:
                page_chunks.append(
                    {
                        "source": pdf_path.name,
                        "page": page_index + 1,
                        "chunk_index": 0,
                        "origin": "page_fallback",
                        "type": "text",
                        "title": f"Текст страницы {page_index + 1}",
                        "content": clean_content(page_text),
                        "data": None,
                        "text": "",
                    }
                )

            for image_index, image in enumerate(images):
                image_path = render_embedded_image(
                    doc, image["xref"], page_index, image_index, out_dir
                )
                created_images.append(image_path)

                print(f"page {page_index + 1}: embedded image {image_index + 1}")
                page_chunks.extend(
                    extract_chunks(
                        image_path,
                        pdf_path.name,
                        page_index + 1,
                        "image",
                        start_index=0,
                    )
                )

            chunks.extend(
                postprocess_page_chunks(page_chunks, pdf_path.name, page_index + 1)
            )
    finally:
        doc.close()
        for image_path in created_images:
            image_path.unlink(missing_ok=True)

    jsonl_path, md_path = write_outputs(chunks, pdf_path, out_dir)
    print(f"Saved {len(chunks)} chunks to {jsonl_path}")
    print(f"Human-readable preview: {md_path}")
    return chunks


def main():
    parser = argparse.ArgumentParser(
        description="Parse a PDF into vision-generated RAG chunks."
    )
    parser.add_argument("pdf", help="PDF path")
    parser.add_argument(
        "--out-dir", default=None, help="Output directory (default: data/processed)"
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
