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
_client_lock = threading.Lock()

_stats_lock = threading.Lock()
_stats: dict[str, int] = {}


def _empty_stats() -> dict[str, int]:
    return {
        "vision_calls": 0,
        "text_calls": 0,
        "cache_hits": 0,
        "failures": 0,
        "vision_ms": 0,
        "text_ms": 0,
    }


def reset_stats() -> None:
    """Reset per-extraction counters used for logging and ImportRun rows."""
    with _stats_lock:
        _stats.clear()
        _stats.update(_empty_stats())


def get_stats() -> dict[str, int]:
    """Return a snapshot of model-call counters for the current extraction."""
    with _stats_lock:
        snapshot = _empty_stats()
        snapshot.update(_stats)
        snapshot["model_calls"] = (
            snapshot["vision_calls"] + snapshot["text_calls"]
        )
        return snapshot


def _record_call(
    kind: str,
    elapsed_ms: float,
    *,
    cache_hit: bool = False,
    failed: bool = False,
) -> None:
    with _stats_lock:
        if cache_hit:
            _stats["cache_hits"] = _stats.get("cache_hits", 0) + 1
            return
        _stats[f"{kind}_calls"] = _stats.get(f"{kind}_calls", 0) + 1
        key = f"{kind}_ms"
        _stats[key] = _stats.get(key, 0) + max(0, round(elapsed_ms))
        if failed:
            _stats["failures"] = _stats.get("failures", 0) + 1


reset_stats()

env = Environment(loader=FileSystemLoader(PROMPTS_DIR))
# Normalized-element flow used by app.rag.extract.
normalized_chunker_prompt = env.get_template("vision_chunker.j2")
normalized_analysis_prompt = env.get_template("image_analysis.j2")


def get_client() -> OpenAI:
    """Create the OpenAI-compatible client once, on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAI(
                    base_url=SETTINGS.llm_base_url,
                    api_key=SETTINGS.llm_api_key,
                    timeout=SETTINGS.extraction_request_timeout,
                    max_retries=2,
                    default_headers={
                        "x-opencode-session": SETTINGS.llm_session_id,
                        "User-Agent": SETTINGS.llm_user_agent,
                    },
                )
    return _client


def _cache_key(kind: str, prompt: str, payload: bytes = b"") -> str:
    digest = hashlib.sha256()
    parts = (
        str(CACHE_SCHEMA),
        kind,
        SETTINGS.llm_base_url,
        MODEL_ID,
        str(MAX_TOKENS),
        "0.0",
        SETTINGS.llm_extraction_reasoning_effort,
        SETTINGS.llm_session_id,
    )
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    digest.update(prompt.encode("utf-8"))
    digest.update(b"\0")
    if payload:
        digest.update(payload)
    return digest.hexdigest()


def _cache_path(key: str) -> Path:
    return SETTINGS.extraction_cache_dir / key[:2] / f"{key}.json"


def _cache_read(key: str) -> str | None:
    if not SETTINGS.extraction_cache_enabled:
        return None
    try:
        item = json.loads(_cache_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        isinstance(item, dict)
        and item.get("schema") == CACHE_SCHEMA
        and isinstance(item.get("raw"), str)
    ):
        return item["raw"]
    return None


def _cache_write(key: str, raw: str) -> None:
    if not SETTINGS.extraction_cache_enabled or not raw.strip():
        return
    path = _cache_path(key)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(
            json.dumps(
                {"schema": CACHE_SCHEMA, "raw": raw},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError:
        logger.debug("Could not write vision cache entry %s", path)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


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
    key = _cache_key("text", prompt)
    started = time.monotonic()
    cached = _cache_read(key)
    if cached is not None:
        _record_call("text", 0, cache_hit=True)
        logger.debug("text cache hit (%d chars)", len(cached))
        return cached
    try:
        raw = _chat_text([{"role": "user", "content": prompt}])
    except Exception:
        _record_call(
            "text", (time.monotonic() - started) * 1000, failed=True
        )
        raise
    elapsed_ms = (time.monotonic() - started) * 1000
    _record_call("text", elapsed_ms)
    _cache_write(key, raw)
    logger.debug("text call finished in %.1fs", elapsed_ms / 1000)
    return raw


def run_vision(prompt: str, image_path) -> str:
    path = Path(image_path)
    image_bytes = path.read_bytes()
    key = _cache_key("vision", prompt, image_bytes)
    started = time.monotonic()
    cached = _cache_read(key)
    if cached is not None:
        _record_call("vision", 0, cache_hit=True)
        logger.debug("vision cache hit for %s", path.name)
        return cached

    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    try:
        raw = _chat_text(
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
    except Exception:
        _record_call(
            "vision", (time.monotonic() - started) * 1000, failed=True
        )
        raise
    elapsed_ms = (time.monotonic() - started) * 1000
    _record_call("vision", elapsed_ms)
    _cache_write(key, raw)
    logger.debug(
        "vision call finished in %.1fs for %s (%d bytes)",
        elapsed_ms / 1000,
        path.name,
        len(image_bytes),
    )
    return raw


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
