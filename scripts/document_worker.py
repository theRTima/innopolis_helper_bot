"""Process admin uploads through the existing immutable RAG pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.rag.extract import extract_version
from app.rag.ingest import ingest_document
from app.rag.render import render_version
from app.storage.models import Document, DocumentImportJob, DocumentVersion, ImportRun

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("document_worker")
DATABASE_URL = os.environ["DATABASE_URL"]
DOCUMENTS_ROOT = Path(os.getenv("DOCUMENTS_DIR", "data/processed/documents"))
POLL_SECONDS = max(1, int(os.getenv("DOCUMENT_WORKER_POLL_SECONDS", "3")))


def run_pipeline(source: Path, document_id: str) -> tuple[dict, dict, dict, dict[str, int]]:
    started = time.monotonic()
    stage_started = time.monotonic()
    ingested = ingest_document(source, document_id, storage_root=DOCUMENTS_ROOT)
    ingest_ms = round((time.monotonic() - stage_started) * 1000)
    version_path = Path(ingested["version_path"])
    stage_started = time.monotonic()
    rendered = render_version(version_path)
    render_ms = round((time.monotonic() - stage_started) * 1000)
    stage_started = time.monotonic()
    extracted = extract_version(version_path)
    extract_ms = round((time.monotonic() - stage_started) * 1000)
    return ingested, rendered, extracted, {
        "ingest_ms": ingest_ms,
        "render_ms": render_ms,
        "extract_ms": extract_ms,
        "total_ms": round((time.monotonic() - started) * 1000),
    }


async def save_success(sessions, job_id: int, title: str, result: tuple) -> None:
    ingested, _rendered, extracted, timings = result
    source_manifest = ingested["manifest"]
    chunk_manifest = extracted["manifest"]
    async with sessions() as session, session.begin():
        job = await session.get(DocumentImportJob, job_id)
        document = await session.scalar(
            select(Document).where(Document.slug == source_manifest["document_id"])
        )
        if document is None:
            document = Document(
                slug=source_manifest["document_id"],
                title=title,
                format=source_manifest["format"],
            )
            session.add(document)
            await session.flush()
        else:
            document.title = title
            document.format = source_manifest["format"]

        version_row = await session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.checksum_sha256 == source_manifest["sha256"],
            )
        )
        if version_row is None:
            current_version = await session.scalar(
                select(func.max(DocumentVersion.version)).where(
                    DocumentVersion.document_id == document.id
                )
            )
            version_row = DocumentVersion(
                document_id=document.id,
                version=(current_version or 0) + 1,
                checksum_sha256=source_manifest["sha256"],
                chunks_sha256=chunk_manifest["chunks_sha256"],
                storage_path=ingested["version_path"],
            )
            session.add(version_row)
            await session.flush()
        else:
            version_row.chunks_sha256 = chunk_manifest["chunks_sha256"]
        document.active_version = version_row.version
        session.add(
            ImportRun(
                document_id=document.id,
                document_version_id=version_row.id,
                parser="docling+vision",
                imported=True,
                chunk_count=chunk_manifest["chunk_count"],
                parse_ms=timings["extract_ms"],
                text_ms=(timings["extract_ms"] if source_manifest["format"] == "text" else 0),
                vision_ms=(timings["extract_ms"] if source_manifest["format"] != "text" else 0),
                total_ms=timings["total_ms"],
            )
        )
        job.status = "completed"
        job.error = None
        job.finished_at = datetime.now(timezone.utc)


async def set_failed(sessions, job_id: int, error: Exception) -> None:
    async with sessions() as session, session.begin():
        job = await session.get(DocumentImportJob, job_id)
        if job is not None:
            job.status = "failed"
            job.error = str(error)[-4000:]
            job.finished_at = datetime.now(timezone.utc)


async def claim_job(sessions) -> DocumentImportJob | None:
    async with sessions() as session, session.begin():
        job = await session.scalar(
            select(DocumentImportJob)
            .where(DocumentImportJob.status == "queued")
            .order_by(DocumentImportJob.created_at, DocumentImportJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        job.status = "processing"
        job.started_at = datetime.now(timezone.utc)
        job.error = None
        await session.flush()
        return DocumentImportJob(
            id=job.id,
            document_id=job.document_id,
            title=job.title,
            original_name=job.original_name,
            storage_path=job.storage_path,
            status=job.status,
        )


async def main() -> None:
    engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # Recover a job left in processing if the single worker was restarted.
        async with sessions() as session, session.begin():
            await session.execute(
                DocumentImportJob.__table__.update()
                .where(DocumentImportJob.status == "processing")
                .values(status="queued", started_at=None)
            )
        logger.info("Document worker polling for uploads")
        while True:
            try:
                job = await claim_job(sessions)
                if job is None:
                    await asyncio.sleep(POLL_SECONDS)
                    continue
                source = Path(job.storage_path)
                logger.info("Processing upload job=%s name=%s", job.id, job.original_name)
                try:
                    result = await asyncio.to_thread(
                        run_pipeline, source, job.document_id
                    )
                    await save_success(sessions, job.id, job.title, result)
                    logger.info("Completed upload job=%s", job.id)
                except Exception as error:
                    logger.exception("Upload processing failed job=%s", job.id)
                    await set_failed(sessions, job.id, error)
                finally:
                    source.unlink(missing_ok=True)
                    try:
                        source.parent.rmdir()
                    except OSError:
                        pass
            except Exception:
                logger.exception("Document worker loop failed")
                await asyncio.sleep(POLL_SECONDS)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
