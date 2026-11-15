"""Vision-only alternative for turning a PDF into machine-usable RAG chunks.

The vision model decides where one semantic chunk ends and the next begins, and
transcribes all content -- including diagrams, screenshots, forms and
output-file icons -- into chunks for the Telegram assistant.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path

import pymupdf
from jinja2 import Environment, FileSystemLoader
from openai import OpenAI

from app.config import Settings
from app.ingestion.normalized import build_search_text, flatten

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"

SETTINGS = Settings.from_env()
MODEL_ID = SETTINGS.llm_model
DPI = 200
MAX_TOKENS = 8192

BOILERPLATE_RE = re.compile(
    r"Ошибка! Используйте вкладку.*?(?:–|-)\s*\d+", re.DOTALL
)
HEADER_LINE_RE = re.compile(
    r"^(?:ЕИС\s*[–-]\s*БП Командировка|Exported on\b.*|\d+)$"
)
MAX_CHUNK_CHARS = 2000

_client: OpenAI | None = None

env = Environment(loader=FileSystemLoader(PROMPTS_DIR))
# Legacy image -> chunk flow used by the flat ingestion CLI.
chunker_prompt = env.get_template("legacy/vision_chunker.j2")
analysis_prompt = env.get_template("legacy/image_analysis.j2")
# Normalized-element flow used by app.rag.extract.
normalized_chunker_prompt = env.get_template("vision_chunker.j2")
normalized_analysis_prompt = env.get_template("image_analysis.j2")


def get_client() -> OpenAI:
    """Create the OpenAI-compatible client once, on first use."""
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=SETTINGS.llm_base_url,
            api_key=SETTINGS.llm_api_key,
            default_headers={
                "x-opencode-session": SETTINGS.llm_session_id,
                "User-Agent": SETTINGS.llm_user_agent,
            },
        )
    return _client


def _chat_text(messages: list[dict]) -> str:
    response = get_client().chat.completions.create(
        model=MODEL_ID,
        messages=messages,
        max_tokens=MAX_TOKENS,
        temperature=0.0,
    )
    message = response.choices[0].message
    return (
        message.content
        or getattr(message, "reasoning_content", None)
        or ""
    )


def run_text(prompt: str) -> str:
    return _chat_text([{"role": "user", "content": prompt}])


def run_vision(prompt: str, image_path) -> str:
    path = Path(image_path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return _chat_text(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{encoded}"
                        },
                    },
                ],
            }
        ]
    )


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
