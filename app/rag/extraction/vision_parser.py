"""Remote vision client and shared extraction helpers.

The model is used for picture/page analysis and for text-only element grouping.
No local MLX runtime is involved.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import time
from pathlib import Path

import pymupdf
from jinja2 import Environment, FileSystemLoader
from openai import OpenAI

from app.config import Settings

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"
logger = logging.getLogger(__name__)

SETTINGS = Settings.from_env()
MODEL_ID = SETTINGS.llm_model
DPI = 200
MAX_TOKENS = 8192
CACHE_SCHEMA = 1

BOILERPLATE_RE = re.compile(
    r"Ошибка! Используйте вкладку.*?(?:–|-)\s*\d+", re.DOTALL
)
HEADER_LINE_RE = re.compile(
    r"^(?:ЕИС\s*[–-]\s*БП Командировка|Exported on\b.*|\d+)$"
)

_client: OpenAI | None = None

env = Environment(loader=FileSystemLoader(PROMPTS_DIR))
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
    kwargs = {}
    if SETTINGS.llm_extraction_reasoning_effort:
        kwargs["reasoning_effort"] = (
            SETTINGS.llm_extraction_reasoning_effort
        )
    response = get_client().chat.completions.create(
        model=MODEL_ID,
        messages=messages,
        max_tokens=MAX_TOKENS,
        temperature=0.0,
        **kwargs,
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


def clean_content(content):
    """Drop running headers/footers and Word template noise."""
    content = BOILERPLATE_RE.sub("", content)
    lines = [
        line
        for line in content.splitlines()
        if not HEADER_LINE_RE.match(line.strip())
    ]
    return "\n".join(lines).strip()


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
