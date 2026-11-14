"""Chunk loading and a small BM25 index for the first bot iteration."""

from __future__ import annotations

import json
import hashlib
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import snowballstemmer
from sqlalchemy import select

from app.storage.db import Database
from app.storage.models import Chunk as StoredChunk
from app.storage.models import Document, DocumentVersion

TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document: str
    page_from: int
    page_to: int
    text: str
    title: str = ""
    content: str | None = None
    data: object = None
    origin: str = "openparse"
    kind: str = "text"

    @property
    def page_label(self) -> str:
        if self.page_from == self.page_to:
            return str(self.page_from)
        return f"{self.page_from}-{self.page_to}"

    @property
    def context(self) -> str:
        if self.content is None:
            return self.text
        parts = [self.title] if self.title and self.title not in self.content else []
        parts.append(self.content)
        if self.data is not None:
            parts.append("Структурированные данные: " + json.dumps(self.data, ensure_ascii=False))
        return "\n".join(parts)


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def load_chunks(knowledge_dir: Path) -> list[Chunk]:
    """Use prepared JSONL chunks when available, otherwise parse PDF text."""

    import openparse

    chunks: list[Chunk] = []
    parser = openparse.DocumentParser()
    for pdf in sorted(knowledge_dir.glob("*.pdf")):
        prepared = knowledge_dir / f"{pdf.stem}.chunks.jsonl"
        meta_path = knowledge_dir / f"{pdf.stem}.chunks.meta.json"
        if prepared.is_file() and meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                checksum = hashlib.sha256(pdf.read_bytes()).hexdigest()
                if meta["source_sha256"] != checksum:
                    raise ValueError("PDF изменился после обработки")
                if not meta["complete"] or meta["chunks_sha256"] != hashlib.sha256(prepared.read_bytes()).hexdigest():
                    raise ValueError("Неполный или изменённый набор чанков")
                prepared_chunks = []
                for line_number, line in enumerate(prepared.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    page = int(item["page"])
                    if page < 1 or not item["text"].strip():
                        raise ValueError(f"Некорректный чанк в строке {line_number}")
                    prepared_chunks.append(
                        Chunk(
                            chunk_id=str(item["id"]),
                            document=pdf.stem,
                            page_from=page,
                            page_to=page,
                            text=str(item["text"]),
                            title=str(item.get("title") or ""),
                            content=item.get("content"),
                            data=item.get("data"),
                            origin=str(item.get("origin") or "vision"),
                            kind=str(item.get("type") or "text"),
                        )
                    )
                if len(prepared_chunks) != meta["chunk_count"] or not prepared_chunks:
                    raise ValueError("Не совпадает количество чанков")
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                logger.warning("Игнорирую %s: %s", prepared, error)
            else:
                chunks.extend(prepared_chunks)
                continue
        elif prepared.is_file():
            logger.warning("Игнорирую %s: нет метаданных о версии PDF", prepared)
        document = parser.parse(pdf)
        for index, node in enumerate(document.nodes):
            text = node.text.strip()
            if not text:
                continue
            chunks.append(
                Chunk(
                    chunk_id=f"{pdf.stem}-{index}",
                    document=pdf.stem,
                    page_from=node.start_page,
                    page_to=node.end_page,
                    text=text,
                )
            )
    return chunks


async def load_chunks_from_database(database: Database) -> list[Chunk]:
    """Load chunks belonging to each document's active version."""
    statement = (
        select(Document, DocumentVersion, StoredChunk)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .join(StoredChunk, StoredChunk.document_version_id == DocumentVersion.id)
        .where(DocumentVersion.version == Document.active_version)
        .order_by(Document.slug, StoredChunk.ordinal)
    )

    async with database.session() as session:
        rows = (await session.execute(statement)).all()

    chunks: list[Chunk] = []
    for document, version, stored_chunk in rows:
        if stored_chunk.locator_kind != "pdf_page":
            logger.warning(
                "Пропускаю чанк %s: неподдерживаемый локатор %s",
                stored_chunk.id,
                stored_chunk.locator_kind,
            )
            continue
        try:
            page = int(stored_chunk.locator_value)
        except (TypeError, ValueError):
            logger.warning("Пропускаю чанк %s: некорректная страница", stored_chunk.id)
            continue

        chunks.append(
            Chunk(
                chunk_id=stored_chunk.external_id or f"{document.slug}-v{version.version}-{stored_chunk.ordinal}",
                document=document.slug,
                page_from=page,
                page_to=page,
                text=stored_chunk.text,
                title=stored_chunk.title or "",
                content=stored_chunk.content,
                data=stored_chunk.data,
                origin=stored_chunk.origin or "database",
                kind=stored_chunk.chunk_type or "text",
            )
        )
    return chunks


class SearchIndex:
    """Okapi BM25 over open-parse nodes; easy to replace with Qdrant later."""

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
        """Add cheap prefix matches for Russian word forms the stemmer missed."""

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
                    expanded[candidate] = max(expanded.get(candidate, 0.0), 0.5)
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
