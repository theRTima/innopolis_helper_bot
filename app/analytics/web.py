"""Internal dashboard and reviewed improvement-task creation."""

from __future__ import annotations

import hmac
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy.exc import DBAPIError

from app.analytics.demo import example_records
from app.analytics.report import (
    build_report, build_task_links, load_issue_tasks, load_records,
)
from app.analytics.taxonomy import QUESTION_KINDS, load_areas
from app.config import Settings
from app.storage.db import Database
from app.tickets.payloads import improvement_payload, issue_key

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
    task_enabled = (
        db is not None and bool(settings.ticket_provider) and bool(password)
    )
    show_examples = show_examples or task_enabled
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

    def task_token(area: str, kind: str, issue: str) -> str:
        digest = hmac.new(
            password.encode("utf-8"),
            f"improvement:{issue_key(area, kind, issue)}".encode("utf-8"),
            hashlib.sha256,
        )
        return digest.hexdigest()

    @app.get(
        "/", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
    )
    async def dashboard(days: int = 7) -> HTMLResponse:
        if days not in (7, 14, 30):
            raise HTTPException(422, "Выберите период в 7, 14 или 30 дней.")
        now = datetime.now(UTC)
        data_start = now - timedelta(days=3 * days)
        if db is None:
            records = example_records(now)
            tasks = []
        else:
            try:
                tasks = await load_issue_tasks(db)
                for task in tasks:
                    if task.closed_at is None:
                        continue
                    closed_at = task.closed_at
                    if closed_at.tzinfo is None:
                        closed_at = closed_at.replace(tzinfo=UTC)
                    else:
                        closed_at = closed_at.astimezone(UTC)
                    data_start = min(
                        data_start, closed_at - timedelta(days=days)
                    )
                records = await load_records(
                    db,
                    since=data_start,
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
        task_links = build_task_links(
            records, tasks, days=days, end=now, data_start=data_start,
        )
        return HTMLResponse(
            template.render(
                report=report,
                areas=areas,
                kinds=QUESTION_KINDS,
                days=days,
                demo=demo,
                task_links=task_links,
                task_enabled=task_enabled,
                task_tokens={
                    signal.key: task_token(*signal.key)
                    for signal in report.signals
                } if task_enabled else {},
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

    @app.post(
        "/issues/create-ticket", dependencies=[Depends(require_admin)]
    )
    async def create_issue_ticket(request: Request) -> RedirectResponse:
        if not task_enabled or db is None:
            raise HTTPException(503, "Создание задач не настроено.")
        body = await request.body()
        if len(body) > 4096:
            raise HTTPException(413, "Форма слишком велика.")
        if request.headers.get("content-type", "").split(";", 1)[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "Нужна форма HTML.")
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as error:
            raise HTTPException(
                422, "Некорректная кодировка формы."
            ) from error

        def field(name: str, limit: int) -> str:
            values = fields.get(name, [])
            if len(values) != 1 or len(values[0]) > limit:
                raise HTTPException(422, "Некорректные параметры задачи.")
            return values[0]

        area = field("area", 80)
        kind = field("kind", 80)
        issue = field("issue", 180)
        csrf = field("csrf", 64)
        days_raw = field("days", 2)
        if days_raw not in {"7", "14", "30"}:
            raise HTTPException(422, "Некорректный период.")
        days = int(days_raw)
        if not hmac.compare_digest(csrf, task_token(area, kind, issue)):
            raise HTTPException(403, "Недействительная форма.")

        now = datetime.now(UTC)
        records = await load_records(
            db,
            since=now - timedelta(days=2 * days),
            until=now,
            show_examples=True,
        )
        report = build_report(records, days=days, end=now)
        signal = next(
            (
                item for item in report.signals
                if item.key == (area, kind, issue)
            ),
            None,
        )
        if signal is None:
            raise HTTPException(409, "Проблема больше не является кандидатом.")
        payload = improvement_payload(
            area=area,
            kind=kind,
            issue=issue,
            period=(
                f"{report.start:%d.%m.%Y %H:%M}–"
                f"{report.end:%d.%m.%Y %H:%M} UTC"
            ),
            questions=signal.current.total,
            users=signal.current.users,
            previous=signal.previous,
            unresolved=signal.current.unresolved,
            no_evidence=(
                signal.current.no_evidence + signal.current.unverified
            ),
            examples=signal.examples,
            documents=signal.documents,
            hypothesis=signal.action,
        )
        await db.enqueue_ticket(
            kind="improvement",
            provider=settings.ticket_provider,
            dedupe_key=payload.local_key,
            payload=asdict(payload),
            issue_area=area,
            issue_kind=kind,
            issue_title=issue,
        )
        return RedirectResponse(f"/?days={days}#signals", status_code=303)

    return app


app = create_app()
