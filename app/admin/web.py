"""Read-only local admin for existing bot models."""

from __future__ import annotations

import base64
import binascii
import hmac
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi_amis_admin.admin.extensions.admin import ReadOnlyModelAdmin
from fastapi_amis_admin.admin.settings import Settings
from fastapi_amis_admin.admin.site import AdminSite, FileAdmin

from app.storage.models import Document, Question, User


def create_app() -> FastAPI:
    load_dotenv()
    database_url = os.environ.get("ADMIN_DATABASE_URL", "").strip()
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not database_url or not username or not password:
        raise RuntimeError("Set ADMIN_DATABASE_URL, ADMIN_USERNAME and ADMIN_PASSWORD")

    app = FastAPI(title="Bot administration", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
        try:
            raw = base64.b64decode(encoded, validate=True).decode("utf-8")
            supplied_user, separator, supplied_password = raw.partition(":")
            valid = bool(separator and scheme.lower() == "basic")
            valid = valid and hmac.compare_digest(supplied_user.encode(), username.encode())
            valid = valid and hmac.compare_digest(supplied_password.encode(), password.encode())
        except (ValueError, UnicodeError, binascii.Error):
            valid = False
        if not valid:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Bot admin"'})
        return await call_next(request)

    site = AdminSite(settings=Settings(database_url_async=database_url, language="en_US", site_title="Bot administration"))
    site.unregister_admin(FileAdmin)

    @site.register_admin
    class QuestionAdmin(ReadOnlyModelAdmin):
        page_schema = "Questions"
        model = Question
        list_display = [Question.id, Question.telegram_id, Question.chat_id, Question.question, Question.created_at]

    @site.register_admin
    class DocumentAdmin(ReadOnlyModelAdmin):
        page_schema = "Documents"
        model = Document
        list_display = [Document.id, Document.slug, Document.title, Document.format, Document.active_version]

    @site.register_admin
    class UserAdmin(ReadOnlyModelAdmin):
        page_schema = "Users"
        model = User
        list_display = [User.id, User.telegram_id, User.created_at]

    site.mount_app(app)
    return app
