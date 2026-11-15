import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.analytics.labeling import classify_pending
from app.analytics.report import QuestionRecord, build_report, load_records
from app.analytics.taxonomy import load_areas
from app.analytics.web import create_app
from app.bot.handlers import handle_question
from app.rag.index import Chunk
from app.storage.db import Database
from app.storage.models import QuestionAnalytics


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
        first = await db.save_question(
            telegram_id=101, chat_id=101,
            question="Где найти календарь? @private_user",
            answer="Инструкция [1]",
            sources=[{"document": "БАРС-Офис", "page": "4"}],
            answer_status="with_sources",
        )
        await db.record_escalation(question_id=first, status="requested")
        assert await classify_pending(db, model, areas, limit=20) == (1, 1)
        await db.record_escalation(question_id=first, status="delivered")
        await db.record_escalation(question_id=first, status="failed")

        second = await db.save_question(
            telegram_id=101, chat_id=101, question="Как открыть календарь?",
            answer="Нет данных", sources=[], answer_status="no_evidence",
        )
        await db.save_question(
            telegram_id=202, chat_id=202, question="Куда зайти за календарём?",
            answer="Инструкция [1]",
            sources=[{"document": "БАРС-Офис", "page": "4"}],
            answer_status="with_sources",
        )
        # Only the asker's current rating counts for the question.
        await db.save_feedback(
            question_id=first, telegram_id=101, value="helpful"
        )
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
        [signal] = report.signals
        assert signal.title == "Поиск календаря"
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
        await db.save_question(
            telegram_id=1, chat_id=1, question="Как пройти согласование?",
            answer="Инструкция [1]", sources=[],
        )
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


@pytest.mark.parametrize(
    ("results", "reply", "status"),
    [
        (False, "", "no_evidence"),
        (True, "Смотрите справку [1].", "with_sources"),
        (True, "Смотрите справку [2].", "unverified"),
        (True, RuntimeError("network"), "error"),
    ],
)
async def test_bot_passes_real_answer_status(results, reply, status):
    chunk = Chunk(
        chunk_id="demo:7",
        document="Документ",
        page_from=7,
        page_to=7,
        text="Текст справки",
    )
    matches = [(1, chunk)] if results else []
    index = SimpleNamespace(search=lambda *_, **__: matches)
    llm = SimpleNamespace(answer_question=AsyncMock())
    if isinstance(reply, Exception):
        llm.answer_question.side_effect = reply
    else:
        llm.answer_question.return_value = reply
    message = SimpleNamespace(
        text="Где раздел?", from_user=SimpleNamespace(id=9),
        chat=SimpleNamespace(id=9),
        bot=SimpleNamespace(send_chat_action=AsyncMock()), answer=AsyncMock(),
    )
    db = SimpleNamespace(save_question=AsyncMock(return_value=1))
    settings = SimpleNamespace(
        top_k=2, min_score=0, documents_base_url="", document_link_secret=""
    )
    await handle_question(message, index, llm, db, settings)
    assert db.save_question.call_args.kwargs["answer_status"] == status
    assert message.answer.await_count == 1


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
            question_id = await db.save_question(
                telegram_id=user_id, chat_id=user_id,
                question="A question with a sensitive detail",
                answer="Test", sources=[], answer_status="no_evidence",
            )
            label = "<script>test</script>"
            labels.append((question_id, "checkout", "navigation", label))
        await db.save_question_labels(labels)
    finally:
        await db.close()

    monkeypatch.setenv("ANALYTICS_DEMO", "0")
    monkeypatch.setenv("ANALYTICS_PASSWORD", "test-secret")
    monkeypatch.setenv("ANALYTICS_USERNAME", "admin")
    monkeypatch.setenv("ANALYTICS_SHOW_EXAMPLES", "0")
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
        assert "A question with a sensitive detail" not in result.text
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
