from app.bot.keyboards import feedback_keyboard


def test_feedback_keyboard_callbacks() -> None:
    markup = feedback_keyboard(42)
    buttons = markup.inline_keyboard[0]

    assert [button.text for button in buttons] == ["Помогло", "Не помогло"]
    assert [button.callback_data for button in buttons] == [
        "fb:helpful:42",
        "fb:not_helpful:42",
    ]
