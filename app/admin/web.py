"""Read-only local admin for existing bot models."""

from __future__ import annotations

from html import escape
import hmac
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi_amis_admin.admin.admin import AdminAction
from fastapi_amis_admin.admin.extensions.admin import ReadOnlyModelAdmin
from fastapi_amis_admin.admin.settings import Settings
from fastapi_amis_admin.admin.site import AdminSite, FileAdmin
from fastapi_amis_admin.amis.components import ActionType, PageSchema
from fastapi_amis_admin.crud.utils import AsyncDatabase
from fastapi_amis_admin.utils.translation import i18n
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.admin.access import register_access_admins, register_access_routes
from app.admin.documents import register_document_routes
from app.storage.models import Document, DocumentImportJob, Question, User


ADMIN_DARK_CSS = """
html, body,
.app-wrapper,
.amis-scope {
    background: #0f172a !important;
}
.cxd-Layout-body,
.cxd-Layout-content,
.cxd-Page,
.cxd-Page-main,
.cxd-Page-content,
.cxd-Page-body {
    background: #0f172a !important;
    color: #e2e8f0 !important;
}
.cxd-Page-header,
.cxd-Page-title {
    color: #f8fafc !important;
    border-color: #334155 !important;
}
.cxd-Layout-header,
.cxd-Layout-headerBar {
    background: #0f172a !important;
    color: #f8fafc !important;
    border-color: #334155 !important;
}
.cxd-Divider {
    border-color: #334155 !important;
}
.cxd-Panel,
.cxd-Panel-body,
.cxd-Crud {
    background: #1e293b !important;
    color: #e2e8f0 !important;
    border: 1px solid #475569 !important;
}
.cxd-Panel-heading {
    background: #273449 !important;
    color: #f8fafc !important;
    border-color: #475569 !important;
}
.cxd-Table-table {
    background: #1e293b !important;
    color: #e2e8f0 !important;
    border: 1px solid #475569 !important;
}
.cxd-Table-tr,
.cxd-Table-table tr {
    background: #1e293b !important;
}
.cxd-Table-tr--odd,
.cxd-Table-table tr:nth-child(even) {
    background: #223047 !important;
}
.cxd-Table-table th {
    background: #273449 !important;
    color: #f8fafc !important;
    border-color: #475569 !important;
}
.cxd-Table-table td {
    color: #e2e8f0 !important;
    border-color: #334155 !important;
}
.cxd-Table-heading,
.cxd-Table-footer,
.cxd-Table-foot {
    background: #273449 !important;
    color: #cbd5e1 !important;
    border-color: #475569 !important;
}
.cxd-Property > table > thead > tr > th {
    background: #273449 !important;
    color: #f8fafc !important;
    border-color: #475569 !important;
}
.cxd-Property > table > tbody > tr {
    background: #1e293b !important;
}
.cxd-Property > table > tbody > tr > th {
    background: #223047 !important;
    color: #f8fafc !important;
    border-color: #475569 !important;
}
.cxd-Property > table > tbody > tr > td {
    color: #e2e8f0 !important;
    border-color: #334155 !important;
}
.cxd-Page a,
.cxd-Table a {
    color: #93c5fd !important;
}
.cxd-Button,
.cxd-Button--default {
    background: #273449 !important;
    color: #f8fafc !important;
    border: 1px solid #475569 !important;
}
.cxd-Button:hover,
.cxd-Button--default:hover {
    background: #334155 !important;
    color: #ffffff !important;
    border-color: #64748b !important;
}
.cxd-Button--primary {
    background: #2563eb !important;
    color: #ffffff !important;
    border-color: #3b82f6 !important;
}
.cxd-Button--primary:hover {
    background: #1d4ed8 !important;
    border-color: #60a5fa !important;
}
.cxd-Button--danger {
    background: #b91c1c !important;
    color: #ffffff !important;
    border-color: #ef4444 !important;
}
.cxd-Button--link {
    background: transparent !important;
    color: #93c5fd !important;
    border: 0 !important;
}
.cxd-Button.is-disabled,
.cxd-Button:disabled {
    opacity: 0.5 !important;
}
.cxd-Form-control,
.cxd-Form-input,
.cxd-Form-select,
.cxd-Page input,
.cxd-Page select,
.cxd-Page textarea {
    background: #0f172a !important;
    color: #f8fafc !important;
    border-color: #475569 !important;
}
.cxd-Page input::placeholder,
.cxd-Page textarea::placeholder {
    color: #94a3b8 !important;
}
.cxd-Layout-aside,
.cxd-Layout-brandBar {
    background: #0f172a !important;
    color: #f8fafc !important;
}
.cxd-Layout-aside a,
.cxd-Layout-aside .cxd-AsideNav-item,
.cxd-Layout-aside .cxd-AsideNav-item a,
.cxd-Layout-brandBar a {
    color: #e2e8f0 !important;
}
.cxd-Layout-aside .cxd-AsideNav-item:hover,
.cxd-Layout-aside .cxd-AsideNav-item.is-active {
    background: #1e293b !important;
    color: #ffffff !important;
}
.cxd-Layout-aside .cxd-AsideNav-item.is-active a {
    color: #ffffff !important;
}
"""


class AdminThemeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/admin"):
            return response
        if not response.headers.get("content-type", "").startswith("text/html"):
            return response
        if hasattr(response, "body"):
            body = response.body
        else:
            body = b"".join([chunk async for chunk in response.body_iterator])
        text = body.decode(getattr(response, "charset", "utf-8"))
        if "</head>" not in text:
            return response
        text = text.replace(
            "</head>",
            f"<style data-admin-dark>{ADMIN_DARK_CSS}</style></head>",
            1,
        )
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() != "content-length"
        }
        return Response(
            content=text,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )


class CleanAdminSite(AdminSite):
    """Admin site without the upstream GitHub header and copyright footer."""

    async def _get_page_as_app(self, request: Request):
        app = await super()._get_page_as_app(request)
        app.logo = None
        app.header = None
        app.footer = None
        return app


def create_app() -> FastAPI:
    load_dotenv()
    database_url = os.environ.get("ADMIN_DATABASE_URL", "").strip()
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    access_database_url = os.environ.get("ADMIN_ACCESS_DATABASE_URL", "").strip()
    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
    if not database_url or not access_database_url or not username or not password:
        raise RuntimeError(
            "Set ADMIN_DATABASE_URL, ADMIN_ACCESS_DATABASE_URL, "
            "ADMIN_USERNAME and ADMIN_PASSWORD"
        )
    database_host = os.environ.get("ADMIN_DATABASE_HOST", "").strip()
    if database_host:
        url = make_url(database_url)
        if url.get_backend_name() != "postgresql":
            raise RuntimeError("ADMIN_DATABASE_HOST requires a PostgreSQL URL")
        port = int(os.environ.get("ADMIN_DATABASE_PORT", "5432"))
        database_url = url.set(host=database_host, port=port).render_as_string(hide_password=False)
        access_url = make_url(access_database_url)
        if access_url.get_backend_name() != "postgresql":
            raise RuntimeError("ADMIN_DATABASE_HOST requires a PostgreSQL URL")
        access_database_url = access_url.set(host=database_host, port=port).render_as_string(
            hide_password=False
        )

    app = FastAPI(title="Bot administration", docs_url=None, redoc_url=None, openapi_url=None)
    i18n.set_language("en_US")
    access_engine = create_async_engine(access_database_url, pool_pre_ping=True)
    access_sessions = async_sessionmaker(access_engine, expire_on_commit=False)
    access_db = AsyncDatabase(access_engine, expire_on_commit=False)
    register_access_routes(
        app,
        sessions=access_sessions,
        admin_username=username,
        bot_username=bot_username,
    )
    inbox = Path(os.environ.get("DOCUMENT_INBOX_DIR", "data/inbox"))
    register_document_routes(app, sessions=access_sessions, inbox=inbox)
    app.add_event_handler("shutdown", access_engine.dispose)

    session_cookie = "bot_admin_session"
    session_ttl = 8 * 60 * 60

    def session_signature(expires: str) -> str:
        message = f"{username}:{expires}".encode()
        return hmac.digest(password.encode(), message, "sha256").hex()

    def valid_session(token: str) -> bool:
        expires, separator, signature = token.partition(".")
        if not separator or not expires.isdigit() or int(expires) < int(time.time()):
            return False
        return hmac.compare_digest(signature, session_signature(expires))

    def safe_next(value: str) -> str:
        return value if value.startswith("/admin/") and not value.startswith("//") else "/admin/"

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if valid_session(request.cookies.get(session_cookie, "")):
            return RedirectResponse("/admin/", status_code=303)
        next_path = safe_next(request.query_params.get("next", "/admin/"))
        page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · Bot Admin</title><style>
:root{color-scheme:light;--ink:#172033;--muted:#68748a;--line:#e3e8f0;--blue:#315efb}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
font:15px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);
background:radial-gradient(ellipse at 15% 10%,#e8efff 0,transparent 38%),#f5f7fb}
.card{width:min(100%,420px);padding:38px;border:1px solid #e9edf4;border-radius:18px;background:#fff;
box-shadow:0 18px 55px #25385c14}.brand{display:flex;align-items:center;gap:12px;margin-bottom:30px}
.mark{display:grid;place-items:center;width:42px;height:42px;border-radius:12px;background:#edf1ff;color:var(--blue);font-weight:800}
h1{font-size:23px;letter-spacing:-.4px;margin:0 0 5px}.sub{margin:0;color:var(--muted)}
form{display:grid;gap:17px;margin-top:28px}label{display:grid;gap:7px;font-size:13px;font-weight:650}
input{width:100%;height:45px;padding:0 12px;border:1px solid var(--line);border-radius:9px;font:inherit;outline:none}
input:focus{border-color:var(--blue);box-shadow:0 0 0 3px #315efb1c}button{height:46px;margin-top:3px;border:0;
border-radius:9px;background:var(--blue);color:#fff;font:inherit;font-weight:650;cursor:pointer}
button:hover{background:#244ce0}.foot{margin-top:21px;color:var(--muted);font-size:12px;text-align:center}
.error{padding:10px 12px;border:1px solid #f1d0d0;border-radius:8px;background:#fff6f5;color:#a33;font-size:13px}
</style></head><body><main class="card"><div class="brand"><div class="mark">B</div><div><strong>Bot Admin</strong><div class="sub">Internal knowledge assistant</div></div></div>
<h1>Welcome back</h1><p class="sub">Sign in to view questions, documents and users.</p>
{error}<form method="post" action="/login"><input type="hidden" name="next" value="{next_path}">
<label>Username<input name="username" autocomplete="username" required autofocus></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button></form><div class="foot">Restricted access · Read-only administration</div></main></body></html>"""
        error_html = (
            '<div class="error">The username or password is incorrect. Please try again.</div>'
            if request.query_params.get("error")
            else ""
        )
        page = page.replace("{next_path}", escape(next_path, quote=True)).replace("{error}", error_html)
        return HTMLResponse(page)

    @app.post("/login")
    async def login(request: Request):
        form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        supplied_user = form.get("username", [""])[0]
        supplied_password = form.get("password", [""])[0]
        user_ok = hmac.compare_digest(supplied_user.encode(), username.encode())
        password_ok = hmac.compare_digest(supplied_password.encode(), password.encode())
        if not (user_ok and password_ok):
            next_path = safe_next(form.get("next", ["/admin/"])[0])
            query = urlencode({"error": "1", "next": next_path})
            return RedirectResponse(f"/login?{query}", status_code=303)
        expires = str(int(time.time()) + session_ttl)
        response = RedirectResponse(safe_next(form.get("next", ["/admin/"])[0]), status_code=303)
        response.set_cookie(
            session_cookie,
            f"{expires}.{session_signature(expires)}",
            max_age=session_ttl,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/",
        )
        return response

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.url.path == "/login":
            return await call_next(request)
        if not valid_session(request.cookies.get(session_cookie, "")):
            if "text/html" in request.headers.get("accept", "") and request.method == "GET":
                target = safe_next(request.url.path)
                return RedirectResponse(f"/login?next={target}", status_code=303)
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return await call_next(request)

    @app.post("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(session_cookie, path="/")
        return response

    site = CleanAdminSite(settings=Settings(database_url_async=database_url, language="en_US", site_title="Bot administration"))
    site.unregister_admin(FileAdmin)

    @site.register_admin
    class QuestionAdmin(ReadOnlyModelAdmin):
        page_schema = PageSchema(
            label="Questions", icon="fa fa-question-circle"
        )
        model = Question
        list_display = [Question.id, Question.telegram_id, Question.chat_id, Question.question, Question.created_at]

    @site.register_admin
    class DocumentAdmin(ReadOnlyModelAdmin):
        page_schema = PageSchema(
            label="Documents", icon="fa fa-file-text-o"
        )
        model = Document
        list_display = [Document.id, Document.slug, Document.title, Document.format, Document.active_version]

    @site.register_admin
    class DocumentImportAdmin(ReadOnlyModelAdmin):
        page_schema = PageSchema(
            label="Document upload queue", icon="fa fa-upload"
        )
        model = DocumentImportJob
        list_display = [DocumentImportJob.id, DocumentImportJob.title, DocumentImportJob.original_name, DocumentImportJob.status, DocumentImportJob.progress_percent, DocumentImportJob.progress_stage, DocumentImportJob.created_at, DocumentImportJob.error]
        admin_action_maker = [
            lambda admin: AdminAction(
                admin,
                name="upload_document",
                action=ActionType.Url(
                    label="Upload document",
                    url="/documents/upload",
                    blank=True,
                ),
                flags=["toolbar"],
            )
        ]

    @site.register_admin
    class UserAdmin(ReadOnlyModelAdmin):
        page_schema = PageSchema(label="Users", icon="fa fa-users")
        model = User
        list_display = [User.id, User.telegram_id, User.created_at]

    register_access_admins(site, access_db)

    site.mount_app(app)
    app.add_middleware(BaseHTTPMiddleware, dispatch=access_db.asgi_dispatch)
    app.add_middleware(AdminThemeMiddleware)
    return app
