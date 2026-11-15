"""Chunk loading and a small BM25 index for the first bot iteration."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import snowballstemmer

TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document: str
    page_from: int
    page_to: int
    text: str
    document_id: str | None = None
    version_id: str | None = None
    title: str = ""
    chunk_type: str = "text"
    origin: str = "docling"
    data: dict | None = None
    locator: dict | None = None
    chunk_file: str = field(default="", compare=False)
    asset_ids: tuple[str, ...] = field(default=(), compare=False)
    assets: tuple[dict, ...] = field(default=(), compare=False)

    @property
    def page_label(self) -> str:
        if self.page_from == self.page_to:
            return str(self.page_from)
        return f"{self.page_from}-{self.page_to}"


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _read_chunk_file(chunks_path: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    with chunks_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{chunks_path}:{line_number}: invalid JSON"
                ) from error
            if not isinstance(item, dict):
                raise ValueError(
                    f"{chunks_path}:{line_number}: expected a JSON object"
                )

            chunk_id = item.get("id")
            source = item.get("source")
            page = item.get("page")
            text = item.get("text")
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                raise ValueError(
                    f"{chunks_path}:{line_number}: missing chunk id"
                )
            if not isinstance(source, str) or not source.strip():
                raise ValueError(
                    f"{chunks_path}:{line_number}: missing source"
                )
            if type(page) is not int or page < 1:
                raise ValueError(f"{chunks_path}:{line_number}: invalid page")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(
                    f"{chunks_path}:{line_number}: missing search text"
                )

            page_to = item.get("page_to", page)
            if type(page_to) is not int or page_to < page:
                page_to = page
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    document=Path(source).stem,
                    page_from=page,
                    page_to=page_to,
                    text=text.strip(),
                    chunk_file=str(chunks_path.resolve()),
                    asset_ids=tuple(
                        value
                        for value in item.get("asset_ids", [])
                        if isinstance(value, str)
                    ),
                    assets=tuple(
                        value
                        for value in item.get("assets", [])
                        if isinstance(value, dict)
                    ),
                    document_id=item.get("document_id"),
                    version_id=item.get("version_id"),
                    title=str(item.get("title") or ""),
                    chunk_type=str(item.get("type") or "text"),
                    origin=str(item.get("origin") or "docling"),
                    data=(
                        item.get("data")
                        if isinstance(item.get("data"), dict)
                        else None
                    ),
                    locator=(
                        item.get("locator")
                        if isinstance(item.get("locator"), dict)
                        else None
                    ),
                )
            )
    return chunks


def load_chunks(chunks_dir: Path) -> list[Chunk]:
    """Load flat Docling/Vision JSONL chunks (legacy layout)."""
    chunks: list[Chunk] = []
    for chunks_path in sorted(chunks_dir.glob("*.chunks.jsonl")):
        chunks.extend(_read_chunk_file(chunks_path))
    return chunks


def _latest_chunk_manifest(version_path: Path) -> Path | None:
    candidates: list[tuple[str, Path]] = []
    for manifest_path in version_path.glob("chunks/*/chunk_manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if manifest.get("status") != "ready":
            continue
        if not (manifest_path.parent / "chunks.jsonl").is_file():
            logger.warning(
                "Ignoring chunk manifest without JSONL: %s", manifest_path
            )
            continue
        candidates.append((str(manifest.get("created_at", "")), manifest_path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], str(item[1])))[1]


def load_versioned_chunks(documents_root: Path) -> list[Chunk]:
    """Load the newest ready chunk set from the versioned store."""
    best: dict[str, tuple[str, Path]] = {}
    for manifest_path in documents_root.glob("*/sha256-*/manifest.json"):
        try:
            source_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        document_id = source_manifest.get("document_id")
        if not isinstance(document_id, str):
            continue
        chunk_manifest_path = _latest_chunk_manifest(manifest_path.parent)
        if chunk_manifest_path is None:
            continue
        try:
            chunk_manifest = json.loads(
                chunk_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        created = str(chunk_manifest.get("created_at", ""))
        chunks_file = chunk_manifest_path.parent / "chunks.jsonl"
        if not chunks_file.is_file():
            continue
        current = best.get(document_id)
        if current is None or (created, str(chunks_file)) > (
            current[0],
            str(current[1]),
        ):
            best[document_id] = (created, chunks_file)

    chunks: list[Chunk] = []
    for _created, chunks_file in best.values():
        manifest_path = chunks_file.parent / "chunk_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest.get("chunks_sha256")
        if expected is not None:
            actual = hashlib.sha256(chunks_file.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"Chunk JSONL hash mismatch: {chunks_file}")
        loaded = _read_chunk_file(chunks_file)
        logger.info(
            "Selected chunks: manifest=%s created=%s count=%d types=%s "
            "warnings=%s integrity=%s",
            manifest_path,
            _created,
            len(loaded),
            dict(Counter(chunk.chunk_type for chunk in loaded)),
            manifest.get("warnings", []),
            "verified" if expected is not None else "not_recorded",
        )
        chunks.extend(loaded)
    return chunks


def load_chunks_auto(
    chunks_dir: Path, documents_root: Path
) -> tuple[list[Chunk], str]:
    """Prefer versioned chunks, adding flat chunks for other documents."""
    versioned = load_versioned_chunks(documents_root)
    if not versioned:
        return load_chunks(chunks_dir), "flat"

    covered = {chunk.document for chunk in versioned}
    additions = [
        chunk
        for chunk in load_chunks(chunks_dir)
        if chunk.document not in covered
    ]
    if additions:
        return versioned + additions, "mixed"
    return versioned, "versioned"


def chunk_source_mismatches(
    chunks: list[Chunk], knowledge_dir: Path
) -> tuple[list[str], list[str]]:
    """Return PDFs without chunks and chunk sources without a matching PDF."""
    pdf_sources = {path.stem for path in knowledge_dir.glob("*.pdf")}
    chunk_sources = {chunk.document for chunk in chunks}
    return sorted(pdf_sources - chunk_sources), sorted(
        chunk_sources - pdf_sources
    )


class SearchIndex:
    """Okapi BM25 over preprocessed Docling/Vision chunks."""

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self._stemmer = snowballstemmer.stemmer("russian")
        self._tokens = [self._normalize(chunk.text) for chunk in chunks]
        self._term_freqs = [Counter(tokens) for tokens in self._tokens]
        self._doc_lengths = [len(tokens) for tokens in self._tokens]
        self._avg_length = (
            sum(self._doc_lengths) / len(self._doc_lengths) if chunks else 0.0
        )

        document_frequency: Counter[str] = Counter()
        for frequencies in self._term_freqs:
            document_frequency.update(frequencies.keys())
        total = len(chunks)
        self._idf = {
            term: math.log(1 + (total - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def _normalize(self, text: str) -> list[str]:
        return [self._stemmer.stemWord(term) for term in tokenize(text)]

    def _expand_terms(self, terms: list[str]) -> list[tuple[str, float]]:
        """Add prefix matches for Russian word forms the stemmer missed."""

        expanded: dict[str, float] = {}
        for term in terms:
            if term in self._idf:
                expanded[term] = max(expanded.get(term, 0.0), 1.0)
                continue
            if len(term) < 4:
                continue
            for candidate in self._idf:
                if len(candidate) < 4:
                    continue
                if abs(len(term) - len(candidate)) > 3:
                    continue
                prefix = 0
                for query_char, candidate_char in zip(term, candidate):
                    if query_char != candidate_char:
                        break
                    prefix += 1
                if prefix >= 4:
                    expanded[candidate] = max(
                        expanded.get(candidate, 0.0), 0.5
                    )
        return list(expanded.items())

    def search(
        self,
        query: str,
        *,
        top_k: int = 4,
        min_score: float = 0.0,
    ) -> list[tuple[float, Chunk]]:
        terms = self._normalize(query)
        if not terms or not self.chunks or self._avg_length == 0:
            return []

        expanded_terms = self._expand_terms(terms)
        if not expanded_terms:
            return []

        scored: list[tuple[float, Chunk]] = []
        for index, frequencies in enumerate(self._term_freqs):
            length = self._doc_lengths[index]
            score = 0.0
            for term, weight in expanded_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (
                    1 - self.b + self.b * length / self._avg_length
                )
                score += (
                    weight
                    * self._idf.get(term, 0.0)
                    * frequency
                    * (self.k1 + 1)
                    / denominator
                )
            if score > 0:
                scored.append((score, self.chunks[index]))

        scored.sort(key=lambda item: item[0], reverse=True)
        if min_score and scored and scored[0][0] < min_score:
            return []
        return scored[:top_k]
