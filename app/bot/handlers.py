"""Telegram handlers: start, help, questions and feedback."""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.anchors import Locator, LocatorKind, build_anchor_url
from app.bot.keyboards import feedback_keyboard, with_support_button, without_feedback_buttons
from app.config import Settings
from app.llm.client import LLMClient
from app.rag.index import Chunk, SearchIndex
from app.storage.db import Database

logger = logging.getLogger(__name__)

router = Router(name="main")

FEEDBACK_MESSAGES = {
    "helpful": "Спасибо! Отметил ответ как полезный.",
    "not_helpful": "Спасибо! Отметил, что ответ не помог.",
}
NO_RESULTS_MESSAGE = (
    "В загруженной документации не нашлось подходящих фрагментов, поэтому "
    "я не могу ответить достоверно. Попробуйте переформулировать вопрос или "
    "уточнить, о каком разделе БАРС-Офиса идёт речь."
)
LLM_ERROR_MESSAGE = (
    "Не удалось получить ответ от модели. Проверьте LLM_BASE_URL, "
    "LLM_API_KEY и LLM_MODEL, затем попробуйте ещё раз."
)
MAX_MESSAGE_LENGTH = 3600


def _format_sources(results: list[tuple[float, Chunk]], cited: set[int] | None = None) -> str:
    if not results:
        return ""
    lines = "\n".join(
        f"[{position}] {chunk.document}, стр. {chunk.page_label}"
        for position, (_, chunk) in enumerate(results, start=1)
        if cited is None or position in cited
    )
    return f"\n\nИсточники:\n{lines}" if lines else ""


def _truncate(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    return text[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"


@router.message(CommandStart())
async def handle_start(message: Message, index: SearchIndex) -> None:
    documents = sorted({chunk.document for chunk in index.chunks})
    if documents:
        corpus = "\n".join(f"- {document}" for document in documents)
        corpus_text = f"Сейчас доступны документы:\n{corpus}"
    else:
        corpus_text = "Корпус документов пока не загружен."

    await message.answer(
        "Я помощник первой линии поддержки по внутренней документации "
        "БАРС Груп.\n\n"
        "Задайте вопрос текстом — я найду подходящие фрагменты, отвечу по ним "
        "и покажу документ и страницу.\n\n"
        f"{corpus_text}\n\n"
        "Команды: /help — справка."
    )


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(
        "Как пользоваться ботом:\n"
        "1. Напишите вопрос обычным текстом.\n"
        "2. Бот найдёт фрагменты в загруженных PDF и ответит со ссылкой на "
        "источник.\n"
        "3. Оцените ответ кнопками «Помогло» или «Не помогло».\n\n"
        "Если в документации нет ответа, бот честно об этом скажет."
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_question(
    message: Message,
    index: SearchIndex,
    llm: LLMClient,
    db: Database,
    settings: Settings,
) -> None:
    question = (message.text or "").strip()
    if len(question) < 2:
        await message.answer("Напишите вопрос текстом, пожалуйста.")
        return
    if message.from_user is None:
        return

    await message.bot.send_chat_action(
        chat_id=message.chat.id,
        action=ChatAction.TYPING,
    )
    results = index.search(
        question,
        top_k=settings.top_k,
        min_score=settings.min_score,
    )

    if results:
        try:
            answer = await llm.answer_question(question, results)
        except Exception:
            logger.exception("LLM request failed")
            answer = LLM_ERROR_MESSAGE
    else:
        answer = NO_RESULTS_MESSAGE

    cited = {int(number) for number in re.findall(r"\[(\d+)\]", answer)}
    cited &= set(range(1, len(results) + 1))
    sources = [
        {
            "document": chunk.document,
            "page": chunk.page_label,
            "score": round(score, 4),
        }
        for position, (score, chunk) in enumerate(results, start=1)
        if position in cited
    ]

    question_id: int | None = None
    try:
        question_id = await db.save_question(
            telegram_id=message.from_user.id,
            chat_id=message.chat.id,
            question=question,
            answer=answer,
            sources=sources,
        )
    except Exception:
        logger.exception("Failed to save question")

    reply = _truncate(answer) + _format_sources(results, cited)
    source_links: list[tuple[str, str]] = []
    if settings.documents_base_url and settings.document_link_secret:
        seen: set[tuple[str, int]] = set()
        for position, (_, chunk) in enumerate(results, start=1):
            if position not in cited:
                continue
            key = (chunk.document, chunk.page_from)
            if key in seen:
                continue
            seen.add(key)
            source_links.append(
                (
                    f"Открыть источник [{position}] · стр. {chunk.page_label}",
                    build_anchor_url(
                        settings.documents_base_url,
                        chunk.document,
                        Locator(LocatorKind.PDF_PAGE, chunk.page_from),
                        secret=settings.document_link_secret,
                    ),
                )
            )
    markup = (
        feedback_keyboard(question_id, source_links)
        if question_id is not None or source_links
        else None
    )
    await message.answer(reply, reply_markup=markup)


@router.callback_query(F.data.startswith("fb:"))
async def handle_feedback(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("Некорректная кнопка.")
        return

    _, value, raw_question_id = parts
    if value not in FEEDBACK_MESSAGES:
        await callback.answer("Некорректная кнопка.")
        return

    try:
        question_id = int(raw_question_id)
    except ValueError:
        await callback.answer("Некорректная кнопка.")
        return

    try:
        not_helpful_count = await db.save_feedback(
            question_id=question_id,
            telegram_id=callback.from_user.id,
            value=value,
        )
    except Exception:
        logger.exception("Failed to save feedback")
        await callback.answer("Не удалось сохранить оценку.")
        return

    await callback.answer(FEEDBACK_MESSAGES[value])

    if isinstance(callback.message, Message):
        try:
            markup = without_feedback_buttons(callback.message.reply_markup)
            if (
                value == "not_helpful"
                and not_helpful_count >= settings.support_escalation_threshold
                and settings.support_url
            ):
                markup = with_support_button(markup, settings.support_url)
            await callback.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            logger.debug("Could not remove feedback keyboard", exc_info=True)


@router.message(F.text.startswith("/"))
async def handle_unknown_command(message: Message) -> None:
    await message.answer("Неизвестная команда. Доступны /start и /help.")
