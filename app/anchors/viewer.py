"""Small signed-link PDF viewer for internal knowledge documents."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from app.anchors.links import sign_document_id
from app.anchors.office import render_office


def create_app(
    knowledge_dir: Path | None = None,
    secret: str | None = None,
    documents_dir: Path | None = None,
) -> FastAPI:
    root = (knowledge_dir or Path(os.getenv("KNOWLEDGE_DIR", "data/knowledge"))).resolve()
    processed_root = (
        documents_dir or Path(os.getenv("DOCUMENTS_DIR", "data/processed/documents"))
    ).expanduser().resolve()
    signing_secret = secret if secret is not None else os.getenv("DOCUMENT_LINK_SECRET", "")
    app = FastAPI(title="Knowledge document viewer", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width\">"
            "<title>Document viewer</title>"
            "<body style=\"font-family:system-ui,sans-serif;margin:3rem\">"
            "<h1>Document viewer is running</h1>"
            "<p>Open a document link sent by the bot.</p>"
            "</body></html>"
        )

    def verify(document_id: str, expires: int, signature: str) -> Path:
        if not signing_secret or expires < int(time.time()):
            raise HTTPException(status_code=403, detail="Ссылка недействительна или устарела")
        expected = sign_document_id(document_id, signing_secret, expires)
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=403, detail="Подпись ссылки неверна")
        if not re.fullmatch(r"[^/\\\x00-\x1f]{1,160}", document_id) or document_id in {".", ".."}:
            raise HTTPException(status_code=404, detail="Документ не найден")
        candidates = [
            (root / f"{document_id}{suffix}").resolve()
            for suffix in (".pdf", ".docx", ".xlsx", ".pptx")
        ]
        matches = [path for path in candidates if path.parent == root and path.is_file()]
        if not matches:
            version_candidates = []
            document_root = (processed_root / document_id).resolve()
            if document_root.parent == processed_root and document_root.is_dir():
                for manifest_path in document_root.glob("sha256-*/manifest.json"):
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    original_name = manifest.get("original_path")
                    if manifest.get("document_id") != document_id or not isinstance(original_name, str):
                        continue
                    original = (manifest_path.parent / original_name).resolve()
                    if original.parent == manifest_path.parent and original.is_file() and original.suffix.lower() in {".pdf", ".docx", ".xlsx", ".pptx"}:
                        version_candidates.append((str(manifest.get("created_at", "")), original))
            if version_candidates:
                matches = [max(version_candidates, key=lambda item: item[0])[1]]
        if not matches:
            raise HTTPException(status_code=404, detail="Документ не найден")
        if len(matches) > 1:
            raise HTTPException(status_code=409, detail="ID документа неоднозначен")
        return matches[0]

    def rendered_pdf(source: Path) -> Path:
        version = source.parent
        if (version.parent.parent != processed_root or
                not version.name.startswith("sha256-")):
            raise HTTPException(status_code=404, detail="PDF представление не найдено")
        candidates = []
        for manifest_path in (version / "renders").glob("render-*/render_manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pdf = manifest_path.parent / "document.pdf"
            if (manifest.get("status") == "ready" and
                    manifest.get("source_sha256") == version.name.removeprefix("sha256-") and
                    pdf.is_file()):
                candidates.append((str(manifest.get("created_at", "")), pdf))
        if not candidates:
            raise HTTPException(status_code=404, detail="PDF представление не найдено")
        return max(candidates, key=lambda item: item[0])[1]

    @app.get("/documents/{document_id}", response_class=HTMLResponse)
    async def view_document(
        document_id: str,
        expires: int = Query(...),
        signature: str = Query(...),
        view: str | None = Query(None),
    ) -> HTMLResponse:
        path = verify(document_id, expires, signature)
        if view not in {None, "rendered"}:
            raise HTTPException(status_code=400, detail="Неизвестный вид документа")
        if view == "rendered":
            rendered_pdf(path)
        if path.suffix.lower() != ".pdf" and view is None:
            try:
                response = HTMLResponse(await asyncio.to_thread(render_office, path))
            except (KeyError, ValueError, zipfile.BadZipFile) as error:
                raise HTTPException(
                    status_code=422,
                    detail="Не удалось разобрать документ",
                ) from error
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        safe_id = quote(document_id, safe="")
        title = document_id.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        view_arg = "&amp;view=rendered" if view == "rendered" else ""
        html = f"""<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>html,body,iframe{{width:100%;height:100%;margin:0;border:0;background:#333}}</style>
        <iframe title="Документ" src="/documents/{safe_id}/file?expires={expires}&amp;signature={signature}{view_arg}"
 referrerpolicy="no-referrer"></iframe>
<script>const frame=document.querySelector('iframe');frame.src += location.hash;</script>
</html>"""
        response = HTMLResponse(html)
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/documents/{document_id}/file")
    async def document_file(
        document_id: str,
        expires: int = Query(...),
        signature: str = Query(...),
        view: str | None = Query(None),
    ) -> FileResponse:
        path = verify(document_id, expires, signature)
        if view == "rendered":
            path = rendered_pdf(path)
        elif view is not None:
            raise HTTPException(status_code=400, detail="Неизвестный вид документа")
        if path.suffix.lower() != ".pdf":
            raise HTTPException(
                status_code=415,
                detail="Файловый endpoint предназначен только для PDF",
            )
        response = FileResponse(
            path,
            media_type="application/pdf",
            filename=path.name,
            content_disposition_type="inline",
        )
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app


app = create_app()
