import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from sqlalchemy import select

from app.bot.input_filter import GibberishMute, RateLimiter, SpamCooldown, valid_question
from app.bot import input_filter
from app.storage.db import Database
from app.storage.models import Analitics
from app.bot import handlers
from app.rag.index import SearchIndex
from app.rag.index import Chunk


@pytest.mark.asyncio
async def test_outcomes_and_escalation(tmp_path):
    db = Database(tmp_path / "outcomes.db")
    await db.init()
    ids = []
    for status, delivered in [("with_sources", True), ("with_sources", True),
                              ("no_evidence", True), ("error", True), ("error", False)]:
        qid = await db.save_question(telegram_id=1, chat_id=1, question="вопрос")
        ids.append(qid)
        await db.save_delivery(question_id=qid, answer="ответ", sources=[], status=status, delivered=delivered)
    await db.save_resolution(question_id=ids[0], telegram_id=1, value="resolved")
    await db.save_resolution(question_id=ids[1], telegram_id=1, value="unresolved")
    await db.record_escalation(question_id=ids[0], status="requested")
    await db.record_escalation(question_id=ids[0], status="delivered")
    await db.record_escalation(question_id=ids[1], status="requested")
    await db.record_escalation(question_id=ids[1], status="failed")
    async with db.session() as session:
        rows = (await session.scalars(select(Analitics).order_by(Analitics.question_id))).all()
        assert len(rows) == 5
        assert rows[0].resolution == "resolved" and rows[0].operator_status == "delivered"
        assert rows[1].resolution == "unresolved" and rows[1].operator_status == "failed"
        assert rows[2].answer_status == "no_evidence"
        assert rows[3].answer_status == "error" and rows[3].sent_at is not None
        assert rows[4].delivery_status == "failed" and rows[4].sent_at is None
        assert all(row.received_at is not None for row in rows)
    await db.close()


@pytest.mark.asyncio
async def test_handler_model_and_delivery_errors(tmp_path, monkeypatch):
    db = Database(tmp_path / "handler.db")
    await db.init()
    monkeypatch.setattr(handlers, "rate_limiter", RateLimiter(0))
    chunk = Chunk(chunk_id="c", document="doc", page_from=1, page_to=1, text="мебель")
    monkeypatch.setattr(handlers, "retrieve_context", lambda *args, **kwargs: [(2.0, chunk)])
    message = SimpleNamespace(text="мебель", from_user=SimpleNamespace(id=8),
                              chat=SimpleNamespace(id=9),
                              bot=SimpleNamespace(send_chat_action=AsyncMock()), answer=AsyncMock())
    settings = SimpleNamespace(top_k=2, min_score=0, documents_base_url="",
                               document_link_secret="", support_url="https://example.com/help")
    await handlers.handle_question(message, None, SimpleNamespace(answer_question=AsyncMock(return_value="Ответ [1]")), db, settings)
    await handlers.handle_question(message, None, SimpleNamespace(answer_question=AsyncMock(side_effect=RuntimeError("model"))), db, settings)
    message.answer = AsyncMock(side_effect=RuntimeError("telegram"))
    with pytest.raises(RuntimeError, match="telegram"):
        await handlers.handle_question(message, None, SimpleNamespace(answer_question=AsyncMock(return_value="Ответ [1]")), db, settings)
    async with db.session() as session:
        rows = (await session.scalars(select(Analitics).order_by(Analitics.question_id))).all()
        assert [row.answer_status for row in rows] == ["with_sources", "error", "with_sources"]
        assert [row.delivery_status for row in rows] == ["delivered", "delivered", "failed"]
    await db.close()


def test_input_checks():
    assert valid_question("БЦ")
    assert not valid_question("!!!!!!!!")
    assert not valid_question("ыыыыыыыы")
    assert not valid_question("asdfgh")
    limiter = RateLimiter()
    assert limiter.allow(1, 1)
    assert not limiter.allow(1, 1)
    assert limiter.allow(2, 1)


def test_spam_cooldown_counts_rejected_messages_and_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(input_filter.time, "monotonic", lambda: now[0])
    cooldown = SpamCooldown()
    assert [cooldown.check(1) for _ in range(7)] == ["allowed"] * 7
    assert cooldown.check(1) == "triggered"
    assert cooldown.check(1) == "blocked"
    assert cooldown.check(2) == "allowed"
    now[0] += 30
    assert cooldown.check(1) == "allowed"
    now[0] += 5
    assert cooldown.check(1) == "allowed"
    assert len(cooldown._events[1]) == 1


def test_gibberish_mute_escalation(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(input_filter.time, "monotonic", lambda: now[0])
    mute = GibberishMute()
    for duration in (600, 1800, 3600, 3600):
        for _ in range(9):
            mute.record_invalid(1)
            assert not mute.is_muted(1)
        mute.record_invalid(1)
        assert mute.is_muted(1)
        assert not mute.is_muted(2)
        now[0] += duration - 1
        assert mute.is_muted(1)
        now[0] += 1
        assert not mute.is_muted(1)


@pytest.mark.asyncio
async def test_gibberish_is_silent_and_does_not_touch_database(monkeypatch):
    monkeypatch.setattr(handlers, "gibberish_mute", GibberishMute())
    db = SimpleNamespace(save_question=AsyncMock())
    message = SimpleNamespace(text="!!!!!!!!", from_user=SimpleNamespace(id=1),
                              chat=SimpleNamespace(id=1), answer=AsyncMock(),
                              bot=SimpleNamespace(send_chat_action=AsyncMock()))
    for _ in range(10):
        await handlers.handle_question(message, None, None, db, None)
    message.text = "БЦ сотрудника"
    await handlers.handle_question(message, None, None, db, None)
    db.save_question.assert_not_awaited()
    message.answer.assert_not_awaited()
    message.bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_slashes_use_lexical_filter_and_preserve_commands(monkeypatch):
    mute = GibberishMute()
    monkeypatch.setattr(handlers, "gibberish_mute", mute)
    db = SimpleNamespace(save_question=AsyncMock())
    message = SimpleNamespace(text="////", from_user=SimpleNamespace(id=10),
                              chat=SimpleNamespace(id=1), answer=AsyncMock())
    await handlers.handle_unknown_command(message, None, None, db, None)
    message.answer.assert_not_awaited()
    db.save_question.assert_not_awaited()
    assert mute._counts[10] == 1
    message.text = "/unknown_command"
    await handlers.handle_unknown_command(message, None, None, db, None)
    message.answer.assert_awaited_once()
    forwarded = AsyncMock()
    monkeypatch.setattr(handlers, "handle_question", forwarded)
    message.text = "/?? БЦ сотрудника"
    await handlers.handle_unknown_command(message, None, None, db, None)
    forwarded.assert_awaited_once()


@pytest.mark.asyncio
async def test_four_clarifications_send_specialist_button(tmp_path, monkeypatch):
    db = Database(tmp_path / "clarifications.db")
    await db.init()
    monkeypatch.setattr(handlers, "rate_limiter", RateLimiter(0))
    monkeypatch.setattr(handlers, "spam_cooldown", SpamCooldown())
    message = SimpleNamespace(text="несуществующий термин", from_user=SimpleNamespace(id=8),
                              chat=SimpleNamespace(id=9),
                              bot=SimpleNamespace(send_chat_action=AsyncMock()), answer=AsyncMock())
    settings = SimpleNamespace(top_k=2, min_score=0, documents_base_url="",
                               document_link_secret="", support_url="https://example.com/help")
    for _ in range(4):
        await handlers.handle_question(message, SearchIndex([]), None, db, settings)
    markup = message.answer.call_args.kwargs["reply_markup"]
    assert any(button.url == settings.support_url for row in markup.inline_keyboard for button in row)
    async with db.session() as session:
        rows = (await session.scalars(select(Analitics).order_by(Analitics.question_id))).all()
        assert [row.answer_status for row in rows] == ["no_evidence"] * 4
        assert rows[-1].operator_status == "delivered"
    await db.close()


@pytest.mark.asyncio
async def test_spam_cooldown_records_but_does_not_reply_repeatedly(tmp_path, monkeypatch):
    db = Database(tmp_path / "cooldown.db")
    await db.init()
    monkeypatch.setattr(handlers, "rate_limiter", RateLimiter(0))
    monkeypatch.setattr(handlers, "spam_cooldown", SpamCooldown())
    message = SimpleNamespace(text="несуществующий термин", from_user=SimpleNamespace(id=88),
                              chat=SimpleNamespace(id=9),
                              bot=SimpleNamespace(send_chat_action=AsyncMock()), answer=AsyncMock())
    settings = SimpleNamespace(top_k=2, min_score=0, documents_base_url="",
                               document_link_secret="", support_url="https://example.com/help")
    for _ in range(9):
        await handlers.handle_question(message, SearchIndex([]), None, db, settings)
    assert message.answer.call_count == 8
    async with db.session() as session:
        rows = (await session.scalars(select(Analitics).order_by(Analitics.question_id))).all()
        assert len(rows) == 9
        assert rows[-1].delivery_status == "suppressed"
        assert rows[-1].sent_at is None
    await db.close()
