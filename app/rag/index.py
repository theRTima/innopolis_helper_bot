"""Chunk loading and a small BM25 index for the first bot iteration."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import snowballstemmer

TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document: str
    page_from: int
    page_to: int
    text: str

    @property
    def page_label(self) -> str:
        if self.page_from == self.page_to:
            return str(self.page_from)
        return f"{self.page_from}-{self.page_to}"


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def load_chunks(knowledge_dir: Path) -> list[Chunk]:
    """Parse every PDF in ``knowledge_dir`` into page-aware text chunks."""

    import openparse

    chunks: list[Chunk] = []
    parser = openparse.DocumentParser()
    for pdf in sorted(knowledge_dir.glob("*.pdf")):
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
