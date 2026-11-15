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
        rows.append([
            InlineKeyboardButton(text="Вопрос решён: да", callback_data=f"resolution:resolved:{question_id}"),
            InlineKeyboardButton(text="Нет", callback_data=f"resolution:unresolved:{question_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
