from app.storage.db import Database
from app.storage.models import Analitics, Answer, Conversation, Feedback, Message, Question, User
from sqlalchemy import select


async def test_question_status_and_operator_share_question_id(tmp_path) -> None:
    database = Database(tmp_path / "analytics.db")
    await database.init()
    try:
        question_id = await database.save_question(
            telegram_id=42, chat_id=42, question="Где инструкция?"
        )
        async with database.session() as session:
            question = await session.get(Question, question_id)
            analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
            assert question is not None and question.answer == ""
            assert analytics is not None and analytics.answer_status is None

        await database.save_answer(
            question_id=question_id, answer="Документ не найден", sources=[], status="no_evidence"
        )
        await database.save_operator_status(question_id=question_id, telegram_id=42, status="requested")
        await database.save_operator_status(question_id=question_id, telegram_id=42, status="delivered")
        async with database.session() as session:
            answer = await session.scalar(select(Answer).where(Answer.question_id == question_id))
            analytics = await session.scalar(select(Analitics).where(Analitics.question_id == question_id))
            assert answer is not None and answer.status == "no_evidence"
            assert analytics is not None and analytics.answer_status == "no_evidence"
            assert analytics.operator_status == "delivered"
    finally:
        await database.close()


async def test_save_question_and_feedback(tmp_path) -> None:
    database = Database(tmp_path / "bot.db")
    await database.init()

    question_id = await database.save_question(
        telegram_id=1,
        chat_id=1,
        question="Какие статусы мебели?",
        answer="Занят, свободен, списан.",
        sources=[{"document": "Реестр мебели", "page": "6"}],
    )
    await database.save_feedback(
        question_id=question_id,
        telegram_id=1,
        value="helpful",
    )

    async with database.session() as session:
        row = await session.scalar(select(Feedback).where(Feedback.question_id == question_id))
        user = await session.scalar(select(User).where(User.telegram_id == 1))
        conversation = await session.scalar(select(Conversation).where(Conversation.chat_id == 1))
        messages = (await session.scalars(select(Message).order_by(Message.id))).all()
        saved_answer = await session.scalar(select(Answer).where(Answer.question_id == question_id))
    assert row is not None
    assert row.value == "helpful"
    assert user is not None and conversation is not None
    assert [message.role for message in messages] == ["user", "assistant"]
    assert saved_answer is not None and saved_answer.text == "Занят, свободен, списан."

    await database.save_feedback(question_id=question_id, telegram_id=1, value="not_helpful")
    async with database.session() as session:
        feedback = (await session.scalars(select(Feedback))).all()
        assert len(feedback) == 1
        assert feedback[0].value == "not_helpful"

    await database.close()


async def test_migration_preserves_existing_rows(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE questions (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, "
            "chat_id INTEGER NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, "
            "sources TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);"
            "CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "question_id INTEGER NOT NULL REFERENCES questions(id), telegram_id INTEGER NOT NULL, "
            "value TEXT NOT NULL CHECK (value IN ('helpful', 'not_helpful')), "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (question_id, telegram_id));"
            "INSERT INTO questions (telegram_id, chat_id, question, answer, sources) "
            "VALUES (42, 42, 'старый вопрос', 'старый ответ', '[]');"
            "INSERT INTO feedback (question_id, telegram_id, value) VALUES (1, 42, 'helpful');"
        )

    database = Database(path)
    await database.init()
    async with database.session() as session:
        question = await session.get(Question, 1)
        feedback = await session.scalar(select(Feedback).where(Feedback.question_id == 1))
        assert question is not None and question.question == "старый вопрос"
        assert feedback is not None and feedback.value == "helpful"

    await database.save_feedback(question_id=1, telegram_id=42, value="not_helpful")
    async with database.session() as session:
        feedback = await session.scalar(select(Feedback).where(Feedback.question_id == 1))
        assert feedback is not None and feedback.value == "not_helpful"

    await database.close()
