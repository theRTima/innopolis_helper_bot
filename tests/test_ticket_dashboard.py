"""A reviewer must confirm one redacted task from a recurring issue card."""

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.analytics.web import create_app
from app.storage.db import Database
from app.storage.models import TicketJob


async def test_reviewed_issue_creates_one_redacted_task(tmp_path, monkeypatch):
    path = tmp_path / "bot.sqlite3"
    db = Database(path)
    await db.init()
    try:
        labels = []
        for user_id in (11, 12, 13):
            question_id = await db.save_question(
                telegram_id=user_id,
                chat_id=user_id,
                question="Фильтр не работает, пишите на secret@example.com",
            )
            await db.save_delivery(
                question_id=question_id,
                answer="Пока нет инструкции",
                sources=[],
                status="no_evidence",
                delivered=True,
            )
            labels.append(
                (question_id, "bars_office", "error", "Ошибка фильтра")
            )
        await db.save_question_labels(labels)
    finally:
        await db.close()

    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANALYTICS_DEMO", "0")
    monkeypatch.setenv("ANALYTICS_USERNAME", "reviewer")
    monkeypatch.setenv("ANALYTICS_PASSWORD", "local-test-secret")
    monkeypatch.setenv("ANALYTICS_SHOW_EXAMPLES", "1")
    monkeypatch.setenv("TICKET_PROVIDER", "demo")

    with TestClient(create_app()) as client:
        auth = ("reviewer", "local-test-secret")
        page = client.get("/", auth=auth)
        assert page.status_code == 200
        assert "Подтвердить задачу" in page.text
        assert "secret@example.com" not in page.text
        token_match = re.search(
            r'name="csrf" value="([0-9a-f]{64})"', page.text
        )
        assert token_match is not None
        form = {
            "area": "bars_office",
            "kind": "error",
            "issue": "Ошибка фильтра",
            "days": "7",
            "csrf": token_match.group(1),
        }
        assert client.post(
            "/issues/create-ticket", data=form
        ).status_code == 401
        assert client.post(
            "/issues/create-ticket", data={**form, "csrf": "0" * 64},
            auth=auth,
        ).status_code == 403
        first = client.post(
            "/issues/create-ticket", data=form, auth=auth,
            follow_redirects=False,
        )
        second = client.post(
            "/issues/create-ticket", data=form, auth=auth,
            follow_redirects=False,
        )
        assert first.status_code == second.status_code == 303
        updated = client.get("/", auth=auth)
        assert "Ожидает создания" in updated.text
        assert "Подтвердить задачу" not in updated.text

    db = Database(path)
    await db.init(migrate=False)
    try:
        async with db.session() as session:
            jobs = (await session.scalars(select(TicketJob))).all()
        assert len(jobs) == 1
        assert jobs[0].kind == "improvement"
        assert jobs[0].state == "queued"
        assert "Наблюдения" in jobs[0].payload["description"]
        assert "Гипотеза для проверки" in jobs[0].payload["description"]
        assert "secret@example.com" not in str(jobs[0].payload)
    finally:
        await db.close()
