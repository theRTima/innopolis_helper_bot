import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.analytics.labeling import classify_pending
from app.analytics.report import (
    IssueTaskRecord, QuestionRecord, build_report, build_task_links,
    load_issue_tasks, load_records,
)
from app.analytics.taxonomy import load_areas
from app.analytics.web import create_app
from app.storage.db import Database
from app.storage.models import QuestionAnalytics, TicketJob


async def save_answered(
    db: Database, *, telegram_id: int, question: str,
    answer: str, sources: list[dict], status: str,
) -> int:
    question_id = await db.save_question(
        telegram_id=telegram_id, chat_id=telegram_id, question=question,
    )
    await db.save_delivery(
        question_id=question_id, answer=answer, sources=sources,
        status=status, delivered=True,
    )
    return question_id


async def test_group_reuse_with_real_storage_and_distinct_users(tmp_path):
    class Model:
        async def analyze_support_questions(self, _, payload):
            data = json.loads(payload)
            assert "@private_user" not in payload
            known = data["known_issues"]
            return json.dumps({
                "items": [
                    {
                        "id": question["id"],
                        "area": "bars_office",
                        "kind": "navigation",
                        "issue": None if known else "Поиск календаря",
                        "match": known[0]["id"] if known else None,
                    }
                    for question in data["questions"]
                ] + [
                    {"id": 9999, "area": "bars_office", "kind": "error"}
                ],
            })

    db = Database(tmp_path / "bot.sqlite3")
    await db.init()
    try:
        model = Model()
        areas = load_areas()
        first = await save_answered(
            db, telegram_id=101,
            question="Где найти календарь? @private_user",
            answer="Инструкция [1]",
            sources=[{"document": "БАРС-Офис", "page": "4"}],
            status="with_sources",
        )
        await db.record_escalation(question_id=first, status="requested")
        assert await classify_pending(db, model, areas, limit=20) == (1, 1)
        await db.record_escalation(question_id=first, status="delivered")
        await db.record_escalation(question_id=first, status="failed")
        job, _ = await db.enqueue_ticket(
            kind="support", provider="demo",
            dedupe_key=f"support:{first}",
            payload={"title": "Помощь", "description": "Вопрос клиента"},
            question_id=first, telegram_id=101,
        )
        assert await db.claim_next_ticket(provider="demo") is not None
        await db.mark_ticket_created(
            ticket_id=job.id, external_id="DEMO-1",
            url="demo://tickets/DEMO-1", status="new",
        )

        second = await save_answered(
            db, telegram_id=101, question="Как открыть календарь?",
            answer="Нет данных", sources=[], status="no_evidence",
        )
        third = await save_answered(
            db, telegram_id=202, question="Куда зайти за календарём?",
            answer="Инструкция [1]",
            sources=[{"document": "БАРС-Офис", "page": "4"}],
            status="with_sources",
        )
        await db.save_resolution(
            question_id=third, telegram_id=202, value="resolved"
        )
        # Only the asker's current rating counts for the question.
        await db.save_feedback(
            question_id=first, telegram_id=101, value="helpful"
        )
        with pytest.raises(ValueError):
            await db.save_feedback(
                question_id=first, telegram_id=202, value="not_helpful"
            )
        await db.save_feedback(
            question_id=second, telegram_id=101, value="helpful"
        )
        await db.save_feedback(
            question_id=second, telegram_id=101, value="not_helpful"
        )
        assert await classify_pending(db, model, areas, limit=20) == (2, 2)
        assert await classify_pending(db, model, areas, limit=20) == (0, 0)
        now = datetime.now(UTC)
        rows = await load_records(
            db, since=now - timedelta(days=1), until=now, show_examples=True
        )
        report = build_report(rows, days=7, end=now)
        stats = report.current
        assert (stats.total, stats.users, stats.no_evidence) == (3, 2, 1)
        assert (stats.rated, stats.helpful) == (2, 1)
        assert (stats.delivered, stats.failed, stats.escalations) == (1, 0, 1)
        assert (stats.eligible, stats.confirmed_resolved) == (3, 1)
        [signal] = report.signals
        assert signal.title == "Поиск календаря"
        assert signal.documents == ("БАРС-Офис",)
        assert len(signal.examples) == 2
        assert all("@private_user" not in text for text in signal.examples)
        # An unanalyzable model response cannot manufacture records.
        async with db.session() as session:
            ids = await session.scalars(select(QuestionAnalytics.question_id))
        assert set(ids) == {1, 2, 3}
    finally:
        await db.close()


async def test_bad_model_output_leaves_question_pending(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    await db.init()
    try:
        await db.save_question(telegram_id=1, chat_id=1,
                               question="Как пройти согласование?")
        model = SimpleNamespace(
            analyze_support_questions=AsyncMock(return_value="мимо JSON")
        )
        with pytest.raises(ValueError, match="JSON"):
            await classify_pending(db, model, load_areas(), limit=10)
        async with db.session() as session:
            assert await session.scalar(select(QuestionAnalytics)) is None
    finally:
        await db.close()


def test_growth_uses_equal_periods_and_coverage():
    now = datetime(2026, 11, 15, 9, tzinfo=UTC)
    start = now - timedelta(days=7)
    rows = [
        QuestionRecord(start, n, "account", "access", "Не приходит код")
        for n in range(1, 5)
    ] + [
        QuestionRecord(start, 5, "account", "navigation"),
        QuestionRecord(start - timedelta(seconds=1), 6,
                       "account", "access", "Не приходит код"),
    ] + [
        QuestionRecord(start - timedelta(days=1), n, "account", "navigation")
        for n in range(7, 11)
    ] + [
        QuestionRecord(now, 11, "account", "access", "Не приходит код"),
    ]
    report = build_report(rows, days=7, end=now)
    assert (report.current.total, report.previous.total) == (5, 5)
    assert report.signals[0].rising
    assert report.signals[0].previous == 1
    report = build_report(rows + [QuestionRecord(start, 12)], days=7, end=now)
    assert not report.can_compare_trends
    assert not report.signals[0].rising


def test_confirmed_resolution_excludes_escalated_and_suppressed():
    now = datetime(2026, 11, 15, 9, tzinfo=UTC)
    rows = [
        QuestionRecord(
            now - timedelta(minutes=1), 1, resolution="resolved",
            delivery_status="delivered", answer_status="with_sources",
        ),
        QuestionRecord(
            now - timedelta(minutes=1), 2, resolution="resolved",
            delivery_status="delivered", answer_status="with_sources",
            escalation_status="requested",
        ),
        QuestionRecord(
            now - timedelta(minutes=1), 3, resolution="resolved",
            delivery_status="rate_limited", answer_status="unverified",
        ),
    ]
    counts = build_report(rows, days=7, end=now).current
    assert (counts.total, counts.eligible, counts.confirmed_resolved) == (
        3, 2, 1,
    )
    assert (counts.unresolved, counts.suppressed) == (1, 1)


def test_offered_link_does_not_count_as_requested_support():
    now = datetime(2026, 11, 15, 9, tzinfo=UTC)
    row = QuestionRecord(
        now - timedelta(minutes=1), 1,
        resolution="resolved", delivery_status="delivered",
        answer_status="with_sources", escalation_status="offered",
    )
    counts = build_report([row], days=7, end=now).current
    assert counts.confirmed_resolved == 1
    assert counts.escalations == 0
    assert counts.unresolved == 0


async def test_historic_link_delivery_is_not_a_ticket(tmp_path):
    db = Database(tmp_path / "old.sqlite3")
    await db.init()
    try:
        question_id = await save_answered(
            db, telegram_id=1, question="Где инструкция?",
            answer="Есть инструкция", sources=[], status="unverified",
        )
        await db.record_escalation(
            question_id=question_id, status="delivered"
        )
        await db.save_resolution(
            question_id=question_id, telegram_id=1, value="resolved"
        )
        now = datetime.now(UTC)
        records = await load_records(
            db, since=now - timedelta(days=1), until=now,
            show_examples=False,
        )
        assert records[0].escalation_status == "offered"
        counts = build_report(records, days=7, end=now).current
        assert counts.confirmed_resolved == 1
        assert counts.delivered == 0
    finally:
        await db.close()


def test_task_effect_uses_complete_equal_windows():
    end = datetime(2026, 11, 15, 9, tzinfo=UTC)
    closed = end - timedelta(days=8)
    task = IssueTaskRecord(
        "bars_office", "navigation", "Поиск календаря",
        "BO-12", "https://example.com/BO-12", "done", closed,
    )
    rows = [
        QuestionRecord(closed - timedelta(days=1), 1, "bars_office",
                       "navigation", "Поиск календаря", "no_evidence",
                       resolution="unresolved", delivery_status="delivered"),
        QuestionRecord(closed + timedelta(days=1), 2, "bars_office",
                       "navigation", "Поиск календаря", "with_sources",
                       resolution="resolved", delivery_status="delivered"),
    ]
    key = (task.area, task.kind, task.title)
    links = build_task_links(
        rows, [task], days=7, end=end,
        data_start=end - timedelta(days=20),
    )
    assert (links[key].before_total, links[key].after_total) == (1, 1)
    assert (
        links[key].before_unresolved, links[key].after_unresolved
    ) == (1, 0)
    pending = build_task_links(
        rows, [task], days=14, end=end,
        data_start=end - timedelta(days=30),
    )
    assert pending[key].after_total is None


async def test_issue_task_metadata_is_available_to_dashboard(tmp_path):
    db = Database(tmp_path / "tasks.sqlite3")
    await db.init()
    try:
        async with db.session() as session, session.begin():
            session.add(TicketJob(
                dedupe_key="improvement:calendar",
                kind="improvement", provider="jira",
                issue_area="bars_office", issue_kind="navigation",
                issue_title="Поиск календаря", payload={},
                state="created", external_id="BO-12",
                external_url="https://example.com/BO-12",
            ))
        [task] = await load_issue_tasks(db)
        assert task.title == "Поиск календаря"
        assert task.external_id == "BO-12"
    finally:
        await db.close()


async def test_dashboard_requires_login_and_escapes_stored_labels(
    tmp_path, monkeypatch
):
    path = tmp_path / "bot.sqlite3"
    areas_file = tmp_path / "areas.json"
    areas_file.write_text(
        '{"checkout": "Оформление заказа"}', encoding="utf-8"
    )
    db = Database(path)
    await db.init()
    try:
        labels = []
        for user_id in range(1, 4):
            question_id = await save_answered(
                db, telegram_id=user_id,
                question="A question with a sensitive detail",
                answer="Test", sources=[], status="no_evidence",
            )
            label = "<script>test</script>"
            labels.append((question_id, "checkout", "navigation", label))
        await db.save_question_labels(labels)
        async with db.session() as session, session.begin():
            session.add(TicketJob(
                dedupe_key="improvement:escaped",
                kind="improvement", provider="jira",
                issue_area="checkout", issue_kind="navigation",
                issue_title="<script>test</script>", payload={},
                state="closed", external_id="BO-14",
                external_url="https://example.com/BO-14",
                closed_at=datetime.now(UTC) - timedelta(days=8),
            ))
    finally:
        await db.close()

    monkeypatch.setenv("ANALYTICS_DEMO", "0")
    monkeypatch.setenv("ANALYTICS_PASSWORD", "test-secret")
    monkeypatch.setenv("ANALYTICS_USERNAME", "admin")
    monkeypatch.setenv("ANALYTICS_SHOW_EXAMPLES", "0")
    monkeypatch.setenv("TICKET_PROVIDER", "demo")
    monkeypatch.setenv("ANALYTICS_AREAS_FILE", str(areas_file))
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DATABASE_URL", "")
    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 401
        wrong_login = client.get("/", auth=("other", "test-secret"))
        assert wrong_login.status_code == 401
        result = client.get("/?days=7", auth=("admin", "test-secret"))
        assert result.status_code == 200
        assert "Оформление заказа" in result.text
        assert "&lt;script&gt;test&lt;/script&gt;" in result.text
        assert "После закрытия задач" in result.text
        assert "BO-14" in result.text
        assert "A question with a sensitive detail" not in result.text
        assert 'class="task-form"' not in result.text
        assert result.headers["Cache-Control"] == "private, no-store"
        assert "Размечено 3 из 3 вопросов" in result.text


def test_demo_does_not_touch_the_bot_database(tmp_path, monkeypatch):
    path = tmp_path / "unused.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANALYTICS_DEMO", "1")
    monkeypatch.setenv("ANALYTICS_PASSWORD", "")
    with TestClient(create_app()) as client:
        result = client.get("/")
        assert result.status_code == 200
        assert "Демонстрационные данные" in result.text
        assert "Поиск производственного календаря" in result.text
        assert "Доля растёт" in result.text
        assert client.get("/?days=14").status_code == 200
    assert not path.exists()
