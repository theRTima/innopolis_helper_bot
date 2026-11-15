"""Document upload, deletion and queue pages for the admin panel."""

from __future__ import annotations

import hashlib
import html
import logging
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import (
    Chunk,
    Citation,
    Document,
    DocumentImportJob,
    DocumentVersion,
    ImportRun,
)

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}

logger = logging.getLogger(__name__)


def _page(title: str, body: str, *, message: str = "") -> HTMLResponse:
    notice = (
        f'<p role="status">{html.escape(message)}</p>' if message else ""
    )
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + html.escape(title) + " · Bot Admin</title>"
        "<style>body{margin:0;padding:32px;background:#f5f7fb;color:#172033;"
        "font:15px/1.5 sans-serif}main{max-width:900px;margin:auto;background:white;"
        "padding:32px;border:1px solid #e5eaf2;border-radius:14px}"
        "label{display:block;margin:12px 0 6px;font-weight:600}"
        "input,button{font:inherit;padding:10px}button{border:0;border-radius:8px;"
        "background:#315efb;color:white;cursor:pointer}"
        "button.danger{background:#b91c1c}button.danger:hover{background:#991b1b}"
        "table{width:100%;border-collapse:collapse;margin-top:20px}"
        "th,td{text-align:left;padding:10px;border-bottom:1px solid #e7ebf2;"
        "overflow-wrap:anywhere}a{color:#315efb}</style>"
        "<body><main><h1>" + html.escape(title) + "</h1>" + notice + body
        + "</main></body></html>",
        headers={"Cache-Control": "no-store"},
    )


async def checksum_already_exists(
    session: AsyncSession, checksum: str
) -> bool:
    """Return whether this content is already stored or actively queued.

    Completed queue rows are intentionally ignored: they are history and must
    not block re-uploading a document after its stored version was deleted.
    Existing documents are still detected through ``document_versions``.
    """
    if await session.scalar(
        select(DocumentVersion.id)
        .where(DocumentVersion.checksum_sha256 == checksum)
        .limit(1)
    ):
        return True
    return bool(
        await session.scalar(
            select(DocumentImportJob.id)
            .where(
                DocumentImportJob.checksum_sha256 == checksum,
                DocumentImportJob.status.in_(("queued", "processing")),
            )
            .limit(1)
        )
    )


async def delete_document_records(
    session: AsyncSession, document_id: str
) -> tuple[str, list[str]] | None:
    """Delete a document and its derived rows, returning stored file paths.

    Upload-queue rows are deliberately left untouched so the history remains
    visible after the document itself is removed.
    """
    document = await session.scalar(
        select(Document).where(Document.slug == document_id)
    )
    if document is None:
        return None

    versions = list(
        await session.scalars(
            select(DocumentVersion).where(
                DocumentVersion.document_id == document.id
            )
        )
    )
    version_ids = [version.id for version in versions]
    storage_paths = [
        version.storage_path
        for version in versions
        if version.storage_path
    ]

    if version_ids:
        chunk_ids = select(Chunk.id).where(
            Chunk.document_version_id.in_(version_ids)
        )
        await session.execute(
            delete(Citation).where(Citation.chunk_id.in_(chunk_ids))
        )
        await session.execute(
            delete(Chunk).where(
                Chunk.document_version_id.in_(version_ids)
            )
        )
    await session.execute(
        delete(ImportRun).where(ImportRun.document_id == document.id)
    )
    if version_ids:
        await session.execute(
            delete(DocumentVersion).where(
                DocumentVersion.id.in_(version_ids)
            )
        )
    await session.execute(
        delete(Document).where(Document.id == document.id)
    )
    return document.title, storage_paths


def remove_document_files(
    storage_paths: list[str], documents_root: Path
) -> tuple[list[str], list[str]]:
    """Remove stored version directories strictly below ``documents_root``.

    Returns ``(removed, failed)``. Paths outside the configured root are never
    touched, so a damaged database row cannot delete arbitrary files.
    """
    root = documents_root.expanduser().resolve()
    removed: list[str] = []
    failed: list[str] = []
    for raw_path in storage_paths:
        try:
            path = Path(raw_path).expanduser().resolve()
        except OSError:
            failed.append(raw_path)
            continue
        if path == root or not path.is_relative_to(root):
            logger.warning(
                "Refusing to delete document path outside %s: %s",
                root,
                raw_path,
            )
            failed.append(raw_path)
            continue
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError:
            logger.exception("Could not delete stored document path %s", path)
            failed.append(raw_path)
            continue
        try:
            path.parent.rmdir()
        except OSError:
            pass
        removed.append(raw_path)
    return removed, failed


def register_document_routes(
    app: FastAPI,
    *,
    sessions: async_sessionmaker,
    inbox: Path,
    documents_root: Path | None = None,
) -> None:
    inbox = inbox.expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    documents_root = (
        documents_root
        or Path(os.getenv("DOCUMENTS_DIR", "data/processed/documents"))
    ).expanduser().resolve()

    @app.get("/documents/upload", response_class=HTMLResponse)
    async def document_upload_page(message: str = ""):
        async with sessions() as session:
            jobs = list(
                await session.scalars(
                    select(DocumentImportJob)
                    .order_by(DocumentImportJob.created_at.desc())
                    .limit(50)
                )
            )
        rows = "".join(
            "<tr><td>" + str(job.id) + "</td><td>" + html.escape(job.title)
            + "</td><td>" + html.escape(job.original_name) + "</td><td>"
            + html.escape(job.status) + "</td><td>"
            + str(job.progress_percent) + "%"
            + "</td><td>" + html.escape(job.progress_stage or "")
            + "</td><td>" + html.escape(job.error or "") + "</td><td>"
            + (job.created_at.isoformat() if isinstance(job.created_at, datetime) else "")
            + "</td></tr>"
            for job in jobs
        )
        body = (
            "<p>Upload PDF, DOCX, XLSX or PPTX files. "
            "The file is queued for background parsing and becomes searchable when processing finishes.</p>"
            "<p>Maximum file size: 50 MiB. Uploads are stored in the ignored runtime data folder.</p>"
            '<form method="post" action="/documents/upload" enctype="multipart/form-data">'
            '<label>Document title</label><input name="title" maxlength="500" required>'
            '<label>File</label><input name="document" type="file" required>'
            '<button type="submit">Upload and queue</button></form>'
            '<p><a href="/admin/">Back to administration</a></p>'
            "<h2>Recent uploads</h2><table><thead><tr><th>ID</th><th>Title</th>"
            "<th>File</th><th>Status</th><th>Progress</th><th>Stage</th>"
            "<th>Error</th><th>Submitted</th></tr></thead>"
            "<tbody>" + (rows or '<tr><td colspan="8">No uploads yet</td></tr>')
            + "</tbody></table>"
        )
        return _page("Document uploads", body, message=message)

    @app.post("/documents/upload")
    async def upload_document(
        title: str = Form(..., max_length=500),
        document: UploadFile = File(...),
    ):
        original_name = Path(
            (document.filename or "upload").replace("\\", "/")
        ).name
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            await document.close()
            return RedirectResponse(
                "/documents/upload?message=Unsupported%20file%20type",
                status_code=303,
            )
        title = title.strip()
        if not title:
            await document.close()
            return RedirectResponse(
                "/documents/upload?message=Title%20is%20required",
                status_code=303,
            )

        document_id = "upload-" + secrets.token_hex(12)
        job_dir = inbox / document_id
        job_dir.mkdir(mode=0o700)
        staged_path = job_dir / original_name
        size = 0
        digest = hashlib.sha256()
        try:
            with staged_path.open("xb") as output:
                while block := await document.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_DOCUMENT_BYTES:
                        raise ValueError("File exceeds the 50 MiB limit")
                    digest.update(block)
                    output.write(block)
            if not size:
                raise ValueError("File is empty")
            checksum = digest.hexdigest()
            duplicate = False
            async with sessions() as session, session.begin():
                duplicate = await checksum_already_exists(session, checksum)
                if not duplicate:
                    session.add(
                        DocumentImportJob(
                            document_id=document_id,
                            title=title,
                            original_name=original_name[:500],
                            storage_path=str(staged_path),
                            checksum_sha256=checksum,
                        )
                    )
                else:
                    session.add(
                        DocumentImportJob(
                            document_id=document_id,
                            title=title,
                            original_name=original_name[:500],
                            storage_path="",
                            checksum_sha256=checksum,
                            status="completed",
                            progress_percent=100,
                            progress_stage="file already exists",
                            finished_at=datetime.now(timezone.utc),
                        )
                    )
            if duplicate:
                staged_path.unlink(missing_ok=True)
                job_dir.rmdir()
                return RedirectResponse(
                    "/documents/upload?message="
                    + quote("Same file already exists."),
                    status_code=303,
                )
        except (OSError, ValueError) as error:
            staged_path.unlink(missing_ok=True)
            job_dir.rmdir()
            return RedirectResponse(
                "/documents/upload?message=" + quote(str(error)),
                status_code=303,
            )
        except Exception:
            staged_path.unlink(missing_ok=True)
            job_dir.rmdir()
            raise
        finally:
            await document.close()
        return RedirectResponse(
            "/documents/upload?message=Upload%20queued", status_code=303
        )

    @app.get("/documents/{document_id}/delete", response_class=HTMLResponse)
    async def delete_document_page(document_id: str):
        async with sessions() as session:
            document = await session.scalar(
                select(Document).where(Document.slug == document_id)
            )
        if document is None:
            return RedirectResponse(
                "/documents/upload?message=Document%20not%20found",
                status_code=303,
            )
        title = html.escape(document.title)
        slug = html.escape(document.slug)
        body = (
            f"<p>Delete <strong>{title}</strong> "
            f"(<code>{slug}</code>)?</p>"
            "<p>The document, its chunks and its stored files will be removed "
            "from the knowledge base. The upload queue history is kept, so the "
            "file can be uploaded again later.</p>"
            f'<form method="post" action="/documents/{quote(document.slug)}/delete">'
            '<button type="submit" class="danger">Delete document</button>'
            ' <a href="/admin/DocumentAdmin">Cancel</a></form>'
        )
        return _page("Delete document", body)

    @app.post("/documents/{document_id}/delete")
    async def delete_document(document_id: str):
        try:
            async with sessions() as session, session.begin():
                deleted = await delete_document_records(session, document_id)
            if deleted is None:
                return RedirectResponse(
                    "/documents/upload?message=Document%20not%20found",
                    status_code=303,
                )
            title, storage_paths = deleted
            removed, failed = remove_document_files(
                storage_paths, documents_root
            )
            message = f"Document “{title}” deleted."
            if failed:
                message += (
                    f" {len(failed)} stored path(s) could not be removed; "
                    "check the admin logs."
                )
            logger.info(
                "Deleted document %s (%d stored paths, %d removed, %d failed)",
                document_id,
                len(storage_paths),
                len(removed),
                len(failed),
            )
        except Exception:
            logger.exception("Could not delete document %s", document_id)
            return RedirectResponse(
                "/documents/upload?message=Delete%20failed",
                status_code=303,
            )
        return RedirectResponse(
            "/documents/upload?message=" + quote(message),
            status_code=303,
        )
