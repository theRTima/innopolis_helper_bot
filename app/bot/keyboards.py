"""Inline keyboards used by the bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def feedback_keyboard(
    question_id: int | None,
    source_links: list[tuple[str, str]] | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, url=url)]
        for label, url in (source_links or [])
    ]
    if question_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Помогло",
                    callback_data=f"fb:helpful:{question_id}",
                ),
                InlineKeyboardButton(
                    text="Не помогло",
                    callback_data=f"fb:not_helpful:{question_id}",
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def without_feedback_buttons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """ Func for inline keyboard"""
    if markup is None:
        return None
    rows = [
        [button for button in row if not (button.callback_data or "").startswith("fb:")]
        for row in markup.inline_keyboard
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

def with_support_button(
    markup: InlineKeyboardMarkup | None,
    support_url: str,
    text: str = "Написать специалисту",
    question_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Append a tracked callback when a question ID is available."""

    rows = [list(row) for row in markup.inline_keyboard] if markup else []
    button = (InlineKeyboardButton(text=text, callback_data=f"support:{question_id}")
              if question_id is not None else InlineKeyboardButton(text=text, url=support_url))
    rows.append([button])
    return InlineKeyboardMarkup(inline_keyboard=rows)
