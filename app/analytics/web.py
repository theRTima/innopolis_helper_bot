"""Read-only internal dashboard for the bot's question log."""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy.exc import DBAPIError

from app.analytics.demo import example_records
from app.analytics.report import build_report, load_records
from app.analytics.taxonomy import QUESTION_KINDS, load_areas
from app.config import Settings
from app.storage.db import Database

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = Settings.from_env()
    areas = load_areas()
    demo = os.getenv("ANALYTICS_DEMO") == "1"
    show_examples = demo or os.getenv("ANALYTICS_SHOW_EXAMPLES") == "1"
    username = os.getenv("ANALYTICS_USERNAME", "admin")
    password = os.getenv("ANALYTICS_PASSWORD", "")
    security = HTTPBasic(auto_error=False, realm="Iskra Analytics")
    database_location = settings.database_url or settings.database_path
    db = None if demo else Database(database_location)
    template = Environment(
        loader=PackageLoader("app.analytics", "templates"),
        autoescape=select_autoescape(["html"]),
    ).get_template("dashboard.html")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if db is not None:
            await db.init(migrate=False)
        try:
            yield
        finally:
            if db is not None:
                await db.close()

    app = FastAPI(
        title="Искра · Аналитика",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def require_admin(
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> None:
        if demo and not password:
            return
        if not password:
            raise HTTPException(503, "Задайте ANALYTICS_PASSWORD для панели.")
        if credentials is not None:
            user_matches = hmac.compare_digest(
                credentials.username.encode(), username.encode()
            )
            password_matches = hmac.compare_digest(
                credentials.password.encode(), password.encode()
            )
            if user_matches and password_matches:
                return
        raise HTTPException(
            401,
            "Войдите в панель аналитики.",
            headers={"WWW-Authenticate": 'Basic realm="Iskra Analytics"'},
        )

    @app.get(
        "/", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
    )
    async def dashboard(days: int = 7) -> HTMLResponse:
        if days not in (7, 14, 30):
            raise HTTPException(422, "Выберите период в 7, 14 или 30 дней.")
        now = datetime.now(UTC)
        if db is None:
            records = example_records(now)
        else:
            try:
                records = await load_records(
                    db,
                    since=now - timedelta(days=2 * days),
                    until=now,
                    show_examples=show_examples,
                )
            except DBAPIError as error:
                logger.warning(
                    "Не удалось прочитать базу аналитики (%s).",
                    type(error).__name__,
                )
                raise HTTPException(
                    503,
                    "База недоступна. Проверьте настройки и запустите бота "
                    "или обработчик аналитики для применения миграций.",
                ) from error
        report = build_report(records, days=days, end=now)
        return HTMLResponse(
            template.render(
                report=report,
                areas=areas,
                kinds=QUESTION_KINDS,
                days=days,
                demo=demo,
            ),
            headers={
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "form-action 'self'; frame-ancestors 'none'; "
                    "base-uri 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    return app


app = create_app()
