from app.bot.keyboards import feedback_keyboard, without_feedback_buttons


def test_feedback_keyboard_callbacks() -> None:
    markup = feedback_keyboard(42)
    buttons = markup.inline_keyboard[0]

    assert [button.text for button in buttons] == ["Помогло", "Не помогло"]
    assert [button.callback_data for button in buttons] == [
        "fb:helpful:42",
        "fb:not_helpful:42",
    ]


def test_keyboard_includes_source_link_and_feedback() -> None:
    markup = feedback_keyboard(
        7,
        [("Открыть источник [1] · стр. 2", "https://docs.example/documents/x")],
    )

    assert markup.inline_keyboard[0][0].url == "https://docs.example/documents/x"
    assert markup.inline_keyboard[1][0].callback_data == "fb:helpful:7"

    after_feedback = without_feedback_buttons(markup)
    assert after_feedback is not None
    assert len(after_feedback.inline_keyboard) == 1
    assert after_feedback.inline_keyboard[0][0].url == "https://docs.example/documents/x"


def test_feedback_only_keyboard_disappears_after_vote() -> None:
    assert without_feedback_buttons(feedback_keyboard(42)) is None

def test_support_button_added_after_source_links() -> None:
    from app.bot.keyboards import with_support_button

    markup = feedback_keyboard(
        5, [("Открыть источник [1]", "https://docs.example/documents/x")]
    )
    result = with_support_button(without_feedback_buttons(markup), "https://t.me/support")

    assert [row[0].text for row in result.inline_keyboard] == [
        "Открыть источник [1]",
        "Написать специалисту",
    ]
    assert result.inline_keyboard[-1][0].url == "https://t.me/support"