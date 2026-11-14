"""Small signed-link PDF viewer for internal knowledge documents."""

from __future__ import annotations

import hmac
import os
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
) -> FastAPI:
    root = (knowledge_dir or Path(os.getenv("KNOWLEDGE_DIR", "data/knowledge"))).resolve()
    signing_secret = secret if secret is not None else os.getenv("DOCUMENT_LINK_SECRET", "")
    app = FastAPI(title="Knowledge document viewer", docs_url=None, redoc_url=None)

    def verify(document_id: str, expires: int, signature: str) -> Path:
        if not signing_secret or expires < int(time.time()):
            raise HTTPException(status_code=403, detail="Ссылка недействительна или устарела")
        expected = sign_document_id(document_id, signing_secret, expires)
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=403, detail="Подпись ссылки неверна")
        candidates = [
            (root / f"{document_id}{suffix}").resolve()
            for suffix in (".pdf", ".docx", ".xlsx", ".pptx")
        ]
        matches = [path for path in candidates if path.parent == root and path.is_file()]
        if not matches:
            raise HTTPException(status_code=404, detail="Документ не найден")
        if len(matches) > 1:
            raise HTTPException(status_code=409, detail="ID документа неоднозначен")
        return matches[0]

    @app.get("/documents/{document_id}", response_class=HTMLResponse)
    async def view_document(
        document_id: str,
        expires: int = Query(...),
        signature: str = Query(...),
    ) -> HTMLResponse:
        path = verify(document_id, expires, signature)
        if path.suffix.lower() != ".pdf":
            try:
                response = HTMLResponse(render_office(path))
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
        html = f"""<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>html,body,iframe{{width:100%;height:100%;margin:0;border:0;background:#333}}</style>
<iframe title="Документ" src="/documents/{safe_id}/file?expires={expires}&amp;signature={signature}"
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
    ) -> FileResponse:
        path = verify(document_id, expires, signature)
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
