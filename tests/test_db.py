from app.storage.db import Database


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

    cursor = await database.connection.execute(
        "SELECT value FROM feedback WHERE question_id = ?",
        (question_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["value"] == "helpful"

    await database.close()
