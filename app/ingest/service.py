"""Persist normalized parser output as a versioned source document."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Chunk, Document, DocumentVersion


def calculate_sha256(file_path: Path) -> str:
    """Calculate a file checksum without reading the whole file into memory."""
    hasher = hashlib.sha256()
    with file_path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


async def import_document(
    session: AsyncSession,
    file_path: Path,
    chunks_data: list[dict],
) -> tuple[DocumentVersion, bool]:
    """Save parser chunks in the caller's transaction.

    Returns the active version and whether a new version was imported. An empty
    parser result is rejected so a failed parse cannot replace a working version.
    """
    file_path = file_path.resolve()
    if not file_path.is_file():
        raise FileNotFoundError(file_path)
    if not chunks_data:
        raise ValueError(f"Парсер не вернул чанки для {file_path.name}; импорт отменён")

    checksum = calculate_sha256(file_path)
    slug = file_path.stem
    document = await session.scalar(select(Document).where(Document.slug == slug))
    if document is None:
        document = Document(
            slug=slug,
            title=file_path.stem,
            format=file_path.suffix.lstrip(".").lower(),
        )
        session.add(document)
        await session.flush()

    if document.active_version is not None:
        active = await session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.version == document.active_version,
            )
        )
        if active is not None and active.checksum_sha256 == checksum:
            return active, False

    latest_version = await session.scalar(
        select(func.max(DocumentVersion.version)).where(
            DocumentVersion.document_id == document.id
        )
    )
    version = DocumentVersion(
        document_id=document.id,
        version=(latest_version or 0) + 1,
        checksum_sha256=checksum,
        storage_path=str(file_path),
    )
    session.add(version)
    await session.flush()

    rows = []
    for ordinal, item in enumerate(chunks_data):
        try:
            page = int(item["page"])
            search_text = str(item["text"]).strip()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Некорректный чанк #{ordinal} в {file_path.name}") from error
        if page < 1 or not search_text:
            raise ValueError(f"Пустой текст или некорректная страница у чанка #{ordinal}")

        rows.append(
            Chunk(
                document_version_id=version.id,
                ordinal=ordinal,
                external_id=str(item["id"]) if item.get("id") is not None else None,
                title=str(item.get("title") or "") or None,
                text=search_text,
                content=str(item.get("content") or "") or None,
                data=item.get("data"),
                chunk_type=str(item.get("type") or "text"),
                origin=str(item.get("origin") or "unknown"),
                locator_kind="pdf_page",
                locator_value=str(page),
            )
        )

    session.add_all(rows)
    await session.flush()
    document.active_version = version.version
    await session.flush()
    return version, True
