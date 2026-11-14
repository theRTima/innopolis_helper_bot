"""Normalized document elements and reference-based chunk resolution.

The vision prompts work on *normalized elements*: text, tables and accepted
image enrichments, each with a stable source reference.  The model only groups
references; this module turns those references back into real chunk text,
structured data and metadata.

Keeping this logic deterministic is important: the model must never be the
only place where source text is copied, because a single dropped reference
would silently lose a document fact.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

ALLOWED_CHUNK_TYPES = {
    "text",
    "table",
    "diagram",
    "screenshot",
    "form",
    "image",
    "mixed",
}

STRUCTURED_TYPES = {"table", "diagram", "screenshot", "form", "image"}


def flatten(value: Any) -> str:
    """Flatten nested metadata into searchable text."""
    if isinstance(value, dict):
        return " ".join(flatten(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(flatten(item) for item in value)
    if value is None:
        return ""
    return str(value).strip()


def build_search_text(title: str, content: str, data: Any) -> str:
    parts = [title, content, flatten(data)]
    return "\n".join(part for part in parts if part)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


@dataclass
class SourcePayload:
    """One independently chunkable source: text, table or enrichment."""

    source_reference_id: str
    kind: str
    text: str
    data: dict | None
    page_from: int
    page_to: int
    section_path: str
    origin: str
    asset_ids: list[str] = field(default_factory=list)
    element_id: str = ""

    def as_prompt_dict(self) -> dict:
        return {
            "source_reference_id": self.source_reference_id,
            "kind": self.kind,
            "text": self.text[:1200],
            "has_structured_data": self.data is not None,
            "page": self.page_from,
        }


@dataclass
class NormalizedElement:
    element_id: str
    element_type: str
    text_verbatim: str
    parent_section_id: str | None
    section_path: str
    source_reference_ids: list[str]
    source_regions: list[dict]
    asset_ids: list[str] = field(default_factory=list)
    table: dict | None = None
    diagram: dict | None = None
    ui: dict | None = None
    accepted_enrichments: list[dict] = field(default_factory=list)
    role: str | None = None
    warnings: list[str] = field(default_factory=list)

    def as_prompt_dict(self) -> dict:
        payload = {
            "element_id": self.element_id,
            "type": self.element_type,
            "parent_section_id": self.parent_section_id,
            "section_path": self.section_path,
            "text_verbatim": self.text_verbatim[:2000],
            "source_reference_ids": self.source_reference_ids,
            "source_regions": self.source_regions,
            "asset_ids": self.asset_ids,
            "accepted_enrichments": self.accepted_enrichments,
        }
        if self.table is not None:
            payload["table"] = self.table
        if self.diagram is not None:
            payload["diagram"] = self.diagram
        if self.ui is not None:
            payload["ui"] = self.ui
        if self.role:
            payload["role"] = self.role
        return payload


class NormalizedDocument:
    """Accumulates normalized elements and resolves chunker references."""

    def __init__(self) -> None:
        self.elements: list[NormalizedElement] = []
        self.sources: dict[str, SourcePayload] = {}
        self.source_order: dict[str, int] = {}
        self.warnings: list[str] = []
        self._element_seq = 0
        self._source_seq = 0

    def next_element_id(self) -> str:
        self._element_seq += 1
        return f"e{self._element_seq}"

    def next_source_id(self) -> str:
        self._source_seq += 1
        return f"s{self._source_seq}"

    def add_source(
        self,
        *,
        kind: str,
        text: str,
        data: dict | None,
        page_from: int,
        page_to: int | None = None,
        section_path: str = "",
        origin: str = "docling",
        asset_ids: list[str] | None = None,
        element_id: str = "",
    ) -> str:
        source_id = self.next_source_id()
        self.sources[source_id] = SourcePayload(
            source_reference_id=source_id,
            kind=kind,
            text=text,
            data=data,
            page_from=page_from,
            page_to=page_to or page_from,
            section_path=section_path,
            origin=origin,
            asset_ids=asset_ids or [],
            element_id=element_id,
        )
        self.source_order[source_id] = len(self.elements)
        return source_id

    def add_element(self, element: NormalizedElement) -> None:
        self.elements.append(element)

    def add_text(
        self,
        *,
        text: str,
        page_from: int,
        page_to: int | None = None,
        section_path: str = "",
        parent_section_id: str | None = None,
        source_regions: list[dict] | None = None,
        role: str | None = None,
        origin: str = "docling",
    ) -> NormalizedElement:
        element_id = self.next_element_id()
        source_id = self.add_source(
            kind="text",
            text=text,
            data=None,
            page_from=page_from,
            page_to=page_to,
            section_path=section_path,
            origin=origin,
            element_id=element_id,
        )
        element = NormalizedElement(
            element_id=element_id,
            element_type="text",
            text_verbatim=text,
            parent_section_id=parent_section_id,
            section_path=section_path,
            source_reference_ids=[source_id],
            source_regions=source_regions or [{"page": page_from}],
            role=role,
        )
        self.add_element(element)
        return element

    def add_structured(
        self,
        *,
        element_type: str,
        text: str,
        data: dict | None,
        page_from: int,
        page_to: int | None = None,
        section_path: str = "",
        parent_section_id: str | None = None,
        source_regions: list[dict] | None = None,
        asset_ids: list[str] | None = None,
        accepted_enrichments: list[dict] | None = None,
        table: dict | None = None,
        diagram: dict | None = None,
        ui: dict | None = None,
        origin: str = "vision",
    ) -> NormalizedElement:
        element_id = self.next_element_id()
        source_id = self.add_source(
            kind=element_type,
            text=text,
            data=data,
            page_from=page_from,
            page_to=page_to,
            section_path=section_path,
            origin=origin,
            asset_ids=asset_ids,
            element_id=element_id,
        )
        element = NormalizedElement(
            element_id=element_id,
            element_type=element_type,
            text_verbatim=text,
            parent_section_id=parent_section_id,
            section_path=section_path,
            source_reference_ids=[source_id],
            source_regions=source_regions or [{"page": page_from}],
            asset_ids=asset_ids or [],
            table=table,
            diagram=diagram,
            ui=ui,
            accepted_enrichments=accepted_enrichments or [],
        )
        self.add_element(element)
        return element

    def prompt_elements(self) -> list[dict]:
        return [element.as_prompt_dict() for element in self.elements]

    def _selected_text(self, source_id: str, selector: dict | None) -> str:
        payload = self.sources.get(source_id)
        if payload is None:
            return ""
        if not selector:
            return payload.text

        kind = selector.get("kind")
        if kind in {"table", "full", None}:
            return payload.text
        if kind == "table_rows" and payload.data:
            columns = payload.data.get("columns")
            rows = payload.data.get("rows")
            if isinstance(columns, list) and isinstance(rows, list):
                grid = [columns, *rows]
                start = int(selector.get("start", 0))
                end = int(selector.get("end_exclusive", len(grid)))
                selected = [
                    grid[index]
                    for index in range(max(start, 0), min(end, len(grid)))
                ]
                header_indices = selector.get("include_header_rows") or [0]
                header_rows = [
                    grid[index]
                    for index in header_indices
                    if isinstance(index, int) and 0 <= index < len(grid)
                ]
                for row in reversed(header_rows):
                    if row not in selected:
                        selected.insert(0, row)
                if selected:
                    return _rows_to_markdown(selected)

        self.warnings.append(
            f"unsupported selector {kind!r} for source {source_id}; used full payload"
        )
        return payload.text

    def _chunk_data(self, payloads: list[SourcePayload]) -> dict | None:
        structured = [payload for payload in payloads if payload.data is not None]
        if not structured:
            return None
        if len(structured) == 1 and len(payloads) == 1:
            return structured[0].data
        return {
            "sources": [
                {
                    "source_reference_id": payload.source_reference_id,
                    "type": payload.kind,
                    "data": payload.data,
                }
                for payload in structured
            ]
        }

    def _chunk_type(
        self, requested: Any, payloads: list[SourcePayload]
    ) -> str:
        kinds = {payload.kind for payload in payloads}
        structured = kinds & STRUCTURED_TYPES
        if len(structured) == 1:
            only_structured = next(iter(structured))
            text_payloads = [
                payload for payload in payloads if payload.kind == "text"
            ]
            if not text_payloads or all(
                len(payload.text) <= 150 for payload in text_payloads
            ):
                return only_structured
        if isinstance(requested, str) and requested in ALLOWED_CHUNK_TYPES:
            if requested == "text" and structured:
                return "mixed"
            return requested
        if kinds == {"text"}:
            return "text"
        if len(kinds) == 1:
            kind = next(iter(kinds))
            return kind if kind in ALLOWED_CHUNK_TYPES else "mixed"
        return "mixed"

    def _chunk_origin(self, payloads: list[SourcePayload]) -> str:
        origins = {payload.origin for payload in payloads}
        if origins == {"docling"}:
            return "docling"
        if origins == {"vision"}:
            return "vision"
        if origins == {"text"}:
            return "text"
        return "mixed"

    def resolve_chunks(
        self,
        raw_chunks: list[dict],
        *,
        source_name: str,
        document_id: str,
        version_id: str,
        source_sha256: str,
        artifact: str,
        artifact_sha256: str = "",
    ) -> list[dict]:
        resolved: list[dict] = []
        covered: set[str] = set()
        used: set[str] = set()
        for index, raw in enumerate(raw_chunks):
            if not isinstance(raw, dict):
                continue
            primary_ids = [
                item
                for item in raw.get("source_reference_ids") or []
                if isinstance(item, str) and item in self.sources
            ]
            context_ids = [
                item
                for item in raw.get("context_source_reference_ids") or []
                if isinstance(item, str) and item in self.sources
            ]
            if not primary_ids:
                self.warnings.append(
                    f"chunk {index} has no valid primary source references"
                )
                continue

            selectors = {}
            for selector in raw.get("selectors") or []:
                if isinstance(selector, dict) and isinstance(
                    selector.get("source_reference_id"), str
                ):
                    selectors[selector["source_reference_id"]] = selector

            primary_payloads = [self.sources[item] for item in primary_ids]
            context_payloads = [self.sources[item] for item in context_ids]
            used.update(primary_ids)
            used.update(context_ids)
            section_path = primary_payloads[0].section_path or ""
            parts: list[str] = []
            seen: set[str] = set()

            for payload in context_payloads:
                if payload.source_reference_id not in seen and payload.text:
                    parts.append(payload.text)
                    seen.add(payload.source_reference_id)
            for payload in primary_payloads:
                text = self._selected_text(
                    payload.source_reference_id,
                    selectors.get(payload.source_reference_id),
                )
                if text and text not in parts:
                    parts.append(text)
                    seen.add(payload.source_reference_id)

            content = "\n\n".join(part.strip() for part in parts if part.strip())
            if section_path and section_path not in content:
                content = f"{section_path}\n\n{content}".strip()
            if not content:
                self.warnings.append(f"chunk {index} resolved to empty content")
                continue

            page = min(payload.page_from for payload in primary_payloads)
            page_to = max(payload.page_to for payload in primary_payloads)
            title = _clean_text(raw.get("title"))
            if section_path:
                title = section_path.split(" > ")[-1]
            elif not title:
                first_line = content.splitlines()[0].strip()
                if len(first_line) <= 100:
                    title = first_line

            data = self._chunk_data(primary_payloads)
            chunk_type = self._chunk_type(raw.get("type"), primary_payloads)
            origin = self._chunk_origin(primary_payloads)
            order = min(
                self.source_order.get(payload.source_reference_id, 10**9)
                for payload in primary_payloads
            )
            covered.update(
                payload.source_reference_id for payload in primary_payloads
            )
            resolved.append(
                self._make_chunk(
                    index=index,
                    source_name=source_name,
                    document_id=document_id,
                    version_id=version_id,
                    source_sha256=source_sha256,
                    artifact=artifact,
                    artifact_sha256=artifact_sha256,
                    page=page,
                    page_to=page_to,
                    origin=origin,
                    chunk_type=chunk_type,
                    title=title,
                    content=content,
                    data=data,
                    order=order,
                )
            )
        if used:
            resolved.extend(
                self._missing_source_chunks(
                    used,
                    start_index=len(resolved),
                    source_name=source_name,
                    document_id=document_id,
                    version_id=version_id,
                    source_sha256=source_sha256,
                    artifact=artifact,
                    artifact_sha256=artifact_sha256,
                )
            )
        return resolved

    def fallback_chunks(
        self,
        *,
        source_name: str,
        document_id: str,
        version_id: str,
        source_sha256: str,
        artifact: str,
        artifact_sha256: str = "",
        max_chars: int = 2000,
    ) -> list[dict]:
        """Deterministic grouping used when the model chunker fails."""
        chunks: list[dict] = []
        buffer: list[NormalizedElement] = []
        buffer_chars = 0

        def flush() -> None:
            nonlocal buffer, buffer_chars
            if not buffer:
                return
            parts = [
                element.text_verbatim.strip()
                for element in buffer
                if element.text_verbatim.strip()
            ]
            if parts:
                page = min(
                    payload_page(self.sources, element)
                    for element in buffer
                )
                page_to = max(
                    payload_page_to(self.sources, element)
                    for element in buffer
                )
                section_path = buffer[-1].section_path
                title = section_path.split(" > ")[-1] if section_path else ""
                content = "\n\n".join(parts)
                data = None
                chunk_type = "text"
                origin = (
                    "docling"
                    if all(
                        self.sources[ref].origin == "docling"
                        for element in buffer
                        for ref in element.source_reference_ids
                    )
                    else "mixed"
                )
                chunks.append(
                    self._make_chunk(
                        index=len(chunks),
                        source_name=source_name,
                        document_id=document_id,
                        version_id=version_id,
                        source_sha256=source_sha256,
                        artifact=artifact,
                        artifact_sha256=artifact_sha256,
                        page=page,
                        page_to=page_to,
                        origin=origin,
                        chunk_type=chunk_type,
                        title=title,
                        content=content,
                        data=data,
                        order=min(
                            self.source_order.get(ref, 10**9)
                            for element in buffer
                            for ref in element.source_reference_ids
                        ),
                    )
                )
            buffer = []
            buffer_chars = 0

        for element in self.elements:
            if element.element_type == "text" and element.role == "heading":
                buffer.append(element)
                buffer_chars += len(element.text_verbatim)
                continue

            if element.element_type == "text":
                if buffer and buffer[-1].section_path != element.section_path:
                    flush()
                buffer.append(element)
                buffer_chars += len(element.text_verbatim)
                if buffer_chars >= max_chars:
                    flush()
                continue

            flush()
            for source_id in element.source_reference_ids:
                payload = self.sources.get(source_id)
                if payload is None or not payload.text:
                    continue
                data = payload.data
                chunks.append(
                    self._make_chunk(
                        index=len(chunks),
                        source_name=source_name,
                        document_id=document_id,
                        version_id=version_id,
                        source_sha256=source_sha256,
                        artifact=artifact,
                        artifact_sha256=artifact_sha256,
                        page=payload.page_from,
                        page_to=payload.page_to,
                        origin=payload.origin,
                        chunk_type=(
                            payload.kind
                            if payload.kind in ALLOWED_CHUNK_TYPES
                            else "mixed"
                        ),
                        title=payload.section_path.split(" > ")[-1]
                        if payload.section_path
                        else "",
                        content=payload.text,
                        data=data,
                        order=self.source_order.get(source_id, 10**9),
                    )
                )
        flush()
        return chunks

    def _missing_source_chunks(
        self,
        covered: set[str],
        *,
        start_index: int,
        source_name: str,
        document_id: str,
        version_id: str,
        source_sha256: str,
        artifact: str,
        artifact_sha256: str = "",
    ) -> list[dict]:
        """Create deterministic chunks for sources the model omitted."""
        chunks: list[dict] = []
        buffer: list[NormalizedElement] = []
        buffer_chars = 0

        def flush() -> None:
            nonlocal buffer, buffer_chars
            if not buffer:
                return
            parts = [
                element.text_verbatim.strip()
                for element in buffer
                if element.text_verbatim.strip()
            ]
            if parts:
                page = min(payload_page(self.sources, el) for el in buffer)
                page_to = max(payload_page_to(self.sources, el) for el in buffer)
                section_path = buffer[-1].section_path
                chunks.append(
                    self._make_chunk(
                        index=start_index + len(chunks),
                        source_name=source_name,
                        document_id=document_id,
                        version_id=version_id,
                        source_sha256=source_sha256,
                        artifact=artifact,
                        artifact_sha256=artifact_sha256,
                        page=page,
                        page_to=page_to,
                        origin="docling",
                        chunk_type="text",
                        title=section_path.split(" > ")[-1]
                        if section_path
                        else "",
                        content="\n\n".join(parts),
                        data=None,
                        order=min(
                            self.source_order.get(ref, 10**9)
                            for element in buffer
                            for ref in element.source_reference_ids
                        ),
                    )
                )
            buffer = []
            buffer_chars = 0

        for element in self.elements:
            missing = [
                ref
                for ref in element.source_reference_ids
                if ref not in covered
            ]
            if not missing:
                continue
            if element.element_type == "text":
                if buffer and buffer[-1].section_path != element.section_path:
                    flush()
                buffer.append(element)
                buffer_chars += len(element.text_verbatim)
                if buffer_chars >= 2000:
                    flush()
                continue

            flush()
            for source_id in missing:
                payload = self.sources.get(source_id)
                if payload is None or not payload.text:
                    continue
                chunks.append(
                    self._make_chunk(
                        index=start_index + len(chunks),
                        source_name=source_name,
                        document_id=document_id,
                        version_id=version_id,
                        source_sha256=source_sha256,
                        artifact=artifact,
                        artifact_sha256=artifact_sha256,
                        page=payload.page_from,
                        page_to=payload.page_to,
                        origin=payload.origin,
                        chunk_type=(
                            payload.kind
                            if payload.kind in ALLOWED_CHUNK_TYPES
                            else "mixed"
                        ),
                        title=payload.section_path.split(" > ")[-1]
                        if payload.section_path
                        else "",
                        content=payload.text,
                        data=payload.data,
                        order=self.source_order.get(source_id, 10**9),
                    )
                )
        flush()
        return chunks

    def _make_chunk(
        self,
        *,
        index: int,
        source_name: str,
        document_id: str,
        version_id: str,
        source_sha256: str,
        artifact: str,
        artifact_sha256: str,
        page: int,
        page_to: int,
        origin: str,
        chunk_type: str,
        title: str,
        content: str,
        data: dict | None,
        order: int = 10**9,
    ) -> dict:
        chunk_id = f"{document_id}:{version_id}:p{page}:c{index}"
        search_text = build_search_text(title, content, data)
        return {
            "id": chunk_id,
            "source": source_name,
            "document_id": document_id,
            "version_id": version_id,
            "source_sha256": source_sha256,
            "artifact": artifact,
            "artifact_sha256": artifact_sha256,
            "page": page,
            "page_to": page_to,
            "chunk_index": index,
            "origin": origin,
            "type": chunk_type,
            "title": title,
            "content": content,
            "data": data,
            "text": search_text,
            "locator": {"kind": "pdf-page", "value": page},
            "_order": order,
        }


def payload_page(sources: dict[str, SourcePayload], element: NormalizedElement) -> int:
    pages = [
        sources[ref].page_from
        for ref in element.source_reference_ids
        if ref in sources
    ]
    return min(pages) if pages else 1


def payload_page_to(
    sources: dict[str, SourcePayload], element: NormalizedElement
) -> int:
    pages = [
        sources[ref].page_to
        for ref in element.source_reference_ids
        if ref in sources
    ]
    return max(pages) if pages else 1


def _heading_only(chunk: dict) -> bool:
    if chunk.get("type") != "text" or chunk.get("data"):
        return False
    content = str(chunk.get("content") or "").strip()
    if not content or len(content) > 120 or len(content.splitlines()) > 2:
        return False
    return not content.endswith((".", "!", "?", ";", ":"))


def _split_text_chunk(chunk: dict, max_chars: int = 1800) -> list[dict]:
    if chunk.get("type") != "text" or chunk.get("data"):
        return [chunk]
    content = str(chunk.get("content") or "")
    if len(content) <= max_chars:
        return [chunk]

    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in content.splitlines():
        if current and size + len(line) + 1 > max_chars:
            parts.append("\n".join(current).strip())
            current = []
            size = 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current).strip())

    result = []
    for part in parts:
        if not part:
            continue
        item = dict(chunk)
        item["content"] = part
        result.append(item)
    return result or [chunk]


def finalize_chunks(
    chunks: list[dict], *, document_id: str, version_id: str
) -> list[dict]:
    """Merge heading-only chunks, split oversized text and assign final IDs."""
    ordered = sorted(
        chunks,
        key=lambda chunk: (
            int(chunk.get("page") or 1),
            int(chunk.get("_order", 10**9)),
        ),
    )
    merged: list[dict] = []
    pending: list[dict] = []
    for chunk in ordered:
        if _heading_only(chunk):
            pending.append(chunk)
            continue
        if pending:
            heading = str(pending[0].get("content") or "").strip()
            if not chunk.get("title"):
                chunk["title"] = heading
            if heading and heading not in chunk["content"]:
                chunk["content"] = f"{heading}\n\n{chunk['content']}"
            for extra in pending[1:]:
                extra_text = str(extra.get("content") or "").strip()
                if extra_text and extra_text not in chunk["content"]:
                    chunk["content"] = f"{extra_text}\n\n{chunk['content']}"
            pending = []
        merged.append(chunk)
    merged.extend(pending)

    split: list[dict] = []
    for chunk in merged:
        split.extend(_split_text_chunk(chunk))

    for index, chunk in enumerate(split):
        chunk["chunk_index"] = index
        chunk["id"] = (
            f"{document_id}:{version_id}:p{chunk.get('page', 1)}:c{index}"
        )
        chunk["text"] = build_search_text(
            str(chunk.get("title") or ""),
            str(chunk.get("content") or ""),
            chunk.get("data"),
        )
        chunk.pop("_order", None)
    return split


def _rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [
        [str(cell) for cell in row] + [""] * (width - len(row))
        for row in rows
    ]
    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
