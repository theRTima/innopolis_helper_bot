"""Document upload page backed by the persistent ingestion queue."""

from __future__ import annotations

import html
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.storage.models import DocumentImportJob

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx"}


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
        "background:#315efb;color:white;cursor:pointer}table{width:100%;"
        "border-collapse:collapse;margin-top:20px}th,td{text-align:left;padding:10px;"
        "border-bottom:1px solid #e7ebf2;overflow-wrap:anywhere}</style>"
        "<body><main><h1>" + html.escape(title) + "</h1>" + notice + body
        + "</main></body></html>",
        headers={"Cache-Control": "no-store"},
    )


def register_document_routes(
    app: FastAPI,
    *,
    sessions: async_sessionmaker,
    inbox: Path,
) -> None:
    inbox = inbox.expanduser().resolve()
    inbox.mkdir(parents=True, exist_ok=True)

    @app.get("/documents/upload", response_class=HTMLResponse)
    async def document_upload_page(message: str = ""):
        async with sessions() as session:
            jobs = list(
                await session.scalars(
                    select(DocumentImportJob).order_by(DocumentImportJob.created_at.desc()).limit(50)
                )
            )
        rows = "".join(
            "<tr><td>" + str(job.id) + "</td><td>" + html.escape(job.title)
            + "</td><td>" + html.escape(job.original_name) + "</td><td>"
            + html.escape(job.status) + "</td><td>"
            + html.escape(job.error or "") + "</td><td>"
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
            "<th>File</th><th>Status</th><th>Error</th><th>Submitted</th></tr></thead>"
            "<tbody>" + (rows or '<tr><td colspan="6">No uploads yet</td></tr>')
            + "</tbody></table>"
        )
        return _page("Document uploads", body, message=message)

    @app.post("/documents/upload")
    async def upload_document(
        title: str = Form(..., max_length=500),
        document: UploadFile = File(...),
    ):
        original_name = Path((document.filename or "upload").replace("\\", "/")).name
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            await document.close()
            return RedirectResponse(
                "/documents/upload?message=Unsupported%20file%20type", status_code=303
            )
        title = title.strip()
        if not title:
            await document.close()
            return RedirectResponse("/documents/upload?message=Title%20is%20required", status_code=303)

        document_id = "upload-" + secrets.token_hex(12)
        job_dir = inbox / document_id
        job_dir.mkdir(mode=0o700)
        staged_path = job_dir / original_name
        size = 0
        try:
            with staged_path.open("xb") as output:
                while block := await document.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_DOCUMENT_BYTES:
                        raise ValueError("File exceeds the 50 MiB limit")
                    output.write(block)
            if not size:
                raise ValueError("File is empty")
            async with sessions() as session, session.begin():
                session.add(
                    DocumentImportJob(
                        document_id=document_id,
                        title=title,
                        original_name=original_name[:500],
                        storage_path=str(staged_path),
                    )
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
        return RedirectResponse("/documents/upload?message=Upload%20queued", status_code=303)
