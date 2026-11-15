"""Chunk loading and a small BM25 index for the first bot iteration."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import snowballstemmer
from app.rag.representation import (
    is_navigation,
    query_representation,
    search_representation,
)

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
    content: str = field(default="", compare=False)

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
                    content=str(item.get("content") or ""),
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


def load_flat_chunks(knowledge_dir: Path) -> list[Chunk]:
    """Load existing PDF sidecars, checking hashes when metadata is present."""
    chunks: list[Chunk] = []
    for pdf in sorted(knowledge_dir.glob("*.pdf")):
        chunk_file = pdf.with_suffix(".chunks.jsonl")
        if not chunk_file.is_file():
            continue
        meta_file = pdf.with_suffix(".chunks.meta.json")
        if meta_file.is_file():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if (not meta.get("complete") or
                    hashlib.sha256(pdf.read_bytes()).hexdigest() != meta.get("source_sha256") or
                    hashlib.sha256(chunk_file.read_bytes()).hexdigest() != meta.get("chunks_sha256")):
                logger.warning("Skipping outdated or incomplete chunks: %s", chunk_file)
                continue
        else:
            logger.warning("Chunk provenance cannot be verified: %s", chunk_file)
        loaded = _read_chunk_file(chunk_file)
        if any(chunk.document != pdf.stem for chunk in loaded):
            logger.warning("Skipping chunks with mismatched source: %s", chunk_file)
            continue
        chunks.extend(loaded)
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
    """Load the newest ready chunk set from the versioned app/rag store."""
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
        if manifest.get("quality", {}).get("status") == "needs_review":
            logger.warning(
                "Chunk set requires review: %s quality=%s",
                manifest_path,
                manifest["quality"],
            )
        chunks.extend(loaded)
    return chunks


class SearchIndex:
    """Okapi BM25 over preprocessed Docling/Vision chunks."""

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        use_search_representation: bool = True,
        documents_root: Path | None = None,
        static_chunks: list[Chunk] | None = None,
    ) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.use_search_representation = use_search_representation
        self.documents_root = documents_root
        self.static_chunks = static_chunks or []
        self._refresh_lock = threading.Lock()
        self._manifest_signature = self._current_manifest_signature()
        self._stemmer = snowballstemmer.stemmer("russian")
        self._tokens = [
            self._normalize(
                search_representation(chunk)
                if use_search_representation
                else chunk.text
            )
            for chunk in chunks
        ]
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

    def _current_manifest_signature(self) -> tuple[tuple[str, int, int], ...]:
        if self.documents_root is None or not self.documents_root.is_dir():
            return ()
        signature = []
        for path in self.documents_root.glob("*/sha256-*/chunks/*/chunk_manifest.json"):
            try:
                stat = path.stat()
            except OSError:
                continue
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(sorted(signature))

    def refresh(self) -> bool:
        """Reload the index after a background worker publishes new chunks."""
        if self.documents_root is None:
            return False
        with self._refresh_lock:
            signature = self._current_manifest_signature()
            if signature == self._manifest_signature:
                return False
            chunks = self.static_chunks + load_versioned_chunks(self.documents_root)
            replacement = SearchIndex(
                chunks,
                k1=self.k1,
                b=self.b,
                use_search_representation=self.use_search_representation,
                documents_root=self.documents_root,
                static_chunks=self.static_chunks,
            )
            replacement._manifest_signature = signature
            replacement._refresh_lock = self._refresh_lock
            self.__dict__ = replacement.__dict__
            logger.info("Refreshed RAG index: %d chunks", len(self.chunks))
            return True

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
        terms = self._normalize(
            query_representation(query)
            if self.use_search_representation
            else query
        )
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
            if (
                self.use_search_representation
                and is_navigation(self.chunks[index])
                and not re.search(r"оглавлен|содержани|contents", query, re.I)
            ):
                score *= 0.15
            if score > 0:
                scored.append((score, self.chunks[index]))

        scored.sort(key=lambda item: item[0], reverse=True)
        if min_score and scored and scored[0][0] < min_score:
            return []

        return scored[:top_k]
