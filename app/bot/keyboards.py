"""Inline keyboards used by the bot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def feedback_keyboard(question_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
        ]
    )
