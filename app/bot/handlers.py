"""Telegram handlers: start, help, questions and feedback."""

from __future__ import annotations

import asyncio
import logging
import math
import re

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from app.anchors import Locator, LocatorKind, build_anchor_url
from app.bot.keyboards import feedback_keyboard
from app.bot.input_filter import (
    GibberishMute, RateLimiter, SpamCooldown, valid_question,
)
from app.config import Settings
from app.llm.client import EMPTY_ANSWER_MESSAGE, LLMClient
from app.llm.guardrails import (
    INVALID_ANSWER,
    NO_EVIDENCE,
    SCOPE_REFUSAL,
    blocked_request,
)
from app.rag.index import Chunk, SearchIndex
from app.rag.retrieve import retrieve_context
from app.storage.db import Database
from app.tickets.service import request_support_ticket

logger = logging.getLogger(__name__)

router = Router(name="main")
rate_limiter = RateLimiter()
spam_cooldown = SpamCooldown()
gibberish_mute = GibberishMute()

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
OPERATOR_REQUEST = re.compile(
    r"(?iu)^\s*(?:пожалуйста[, ]+)?(?:оператор|человек|"
    r"позовите оператора|свяжите с оператором|"
    r"соедините с оператором|можно связаться с оператором|"
    r"можно оператора|(?:мне )?нужен оператор|"
    r"хочу (?:связаться с )?оператором|хочу оператора|"
    r"позовите человека|(?:мне )?нужен человек|"
    r"позовите специалиста)\s*(?:,? пожалуйста)?[.!?]?\s*$"
)


def _ticket_message(job) -> str:
    if job.external_id:
        return (
            f"Заявка {job.external_id} создана. "
            f"Текущий статус: {job.external_status or job.state}."
        )
    if job.state == "uncertain":
        return (
            f"Запрос #{job.id} проверяется: ответ тикет-системы "
            "не получен. Повторную заявку не создаю."
        )
    if job.state == "failed":
        return (
            f"Запрос #{job.id} не удалось передать. "
            "Сообщите администратору номер запроса."
        )
    return (
        f"Запрос #{job.id} принят. Статус: в очереди. "
        "Пришлю номер заявки после создания в тикет-системе."
    )


async def _request_operator(
    message: Message, db: Database, settings: Settings
) -> None:
    if message.from_user is None:
        return
    provider = getattr(settings, "ticket_provider", "")
    if not provider:
        if settings.support_url:
            await message.answer(
                "Связаться со специалистом:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="Открыть поддержку", url=settings.support_url
                    )
                ]]),
            )
            latest = await db.latest_question_id(
                telegram_id=message.from_user.id, chat_id=message.chat.id
            )
            if latest is not None:
                await db.record_escalation(
                    question_id=latest, status="offered"
                )
        else:
            await message.answer("Создание заявок пока не настроено.")
        return
    question_id = None
    replied = getattr(message, "reply_to_message", None)
    markup = getattr(replied, "reply_markup", None)
    if markup is not None:
        for row in markup.inline_keyboard:
            for button in row:
                parts = (button.callback_data or "").split(":")
                if len(parts) == 3 and parts[0] in {"resolution", "fb"}:
                    try:
                        question_id = int(parts[2])
                    except ValueError:
                        pass
                    break
            if question_id is not None:
                break
    if question_id is None:
        text = (message.text or "").strip()
        description = text.partition(" ")[2].strip() if text.startswith("/operator ") else ""
        if not description:
            await message.answer(
                "Опишите проблему командой /operator <описание> или ответьте "
                "командой /operator на сообщение бота с нужным ответом."
            )
            return
        if not valid_question(description):
            await message.answer("Опишите проблему словами, чтобы создать заявку.")
            return
        question_id = await db.save_question(
            telegram_id=message.from_user.id,
            chat_id=message.chat.id,
            question=description,
        )
    job, _ = await request_support_ticket(
        db,
        question_id=question_id,
        telegram_id=message.from_user.id,
        provider=provider,
    )
    await message.answer(_ticket_message(job))


def _source_links(sources: list[dict], settings: Settings) -> list[tuple[str, str]]:
    if not settings.documents_base_url or not settings.document_link_secret:
        return []
    links = []
    seen: set[tuple[str, str, str]] = set()
    for source in sources:
        document_id = source.get("document_id")
        page_from = source.get("page_from")
        if not isinstance(document_id, str):
            continue
        raw_locator = source.get("locator")
        try:
            if isinstance(raw_locator, dict):
                locator = Locator(
                    LocatorKind(raw_locator["kind"]), raw_locator["value"],
                    raw_locator.get("subvalue"),
                )
            elif isinstance(page_from, int):
                locator = Locator(LocatorKind.PDF_PAGE, page_from)
            else:
                continue
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping invalid source locator: %s", document_id)
            continue
        key = (document_id, locator.kind, locator.fragment())
        if key in seen:
            continue
        seen.add(key)
        links.append((
            f"Открыть источник [{source['position']}] · {source.get('page', '')}",
            build_anchor_url(
                settings.documents_base_url,
                document_id,
                locator,
                secret=settings.document_link_secret,
                view=("rendered" if source.get("source_format") in
                      {"docx", "xlsx", "pptx"} and
                      locator.kind == LocatorKind.PDF_PAGE else None),
            ),
        ))
    return links


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
        "Команды: /help — справка, /operator — заявка специалисту."
    )


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(
        "Как пользоваться ботом:\n"
        "1. Напишите вопрос обычным текстом.\n"
        "2. Бот найдёт фрагменты в загруженных PDF и ответит со ссылкой на "
        "источник.\n"
        "3. Отметьте, решён ли ваш вопрос.\n\n"
        "Если в документации нет ответа, бот честно об этом скажет. "
        "Для заявки специалисту используйте /operator."
    )


@router.message(Command("operator"))
async def handle_operator_command(
    message: Message, db: Database, settings: Settings
) -> None:
    if message.from_user and gibberish_mute.is_muted(message.from_user.id):
        return
    await _request_operator(message, db, settings)


@router.message(F.text & ~F.text.startswith("/"))
async def handle_question(
    message: Message,
    index: SearchIndex,
    llm: LLMClient,
    db: Database,
    settings: Settings,
) -> None:
    question = (message.text or "").strip()
    if message.from_user is None:
        return

    if gibberish_mute.is_muted(message.from_user.id):
        return
    if OPERATOR_REQUEST.fullmatch(question):
        gibberish_mute.record_valid(message.from_user.id)
        await _request_operator(message, db, settings)
        return
    if not valid_question(question):
        gibberish_mute.record_invalid(message.from_user.id)
        return
    gibberish_mute.record_valid(message.from_user.id)

    question_id = await db.save_question(
        telegram_id=message.from_user.id,
        chat_id=message.chat.id,
        question=question,
    )
    cooldown_state = spam_cooldown.check(message.from_user.id)
    if cooldown_state == "blocked":
        await db.suppress_delivery(question_id=question_id)
        return
    if cooldown_state == "triggered":
        notice = (
            "Слишком много сообщений: не более "
            f"{spam_cooldown.limit - 1} за {spam_cooldown.window:g} секунд. "
            f"Подождите {spam_cooldown.cooldown:g} секунд и попробуйте снова."
        )
        try:
            await message.answer(notice)
        except Exception:
            await db.save_delivery(
                question_id=question_id, answer=notice, sources=[],
                status="unverified", delivered=False,
                delivery_status="rate_limited",
            )
            raise
        await db.save_delivery(
            question_id=question_id, answer=notice, sources=[],
            status="unverified", delivered=True,
            delivery_status="rate_limited",
        )
        return
    if not rate_limiter.allow(message.from_user.id, message.chat.id):
        wait = max(
            1,
            math.ceil(
                rate_limiter.retry_after(
                    message.from_user.id, message.chat.id
                )
            ),
        )
        notice = (
            "Слишком частые запросы: один вопрос можно отправлять "
            f"раз в {rate_limiter.seconds:g} секунды. "
            f"Подождите {wait} секунд."
        )
        try:
            await message.answer(notice)
        except Exception:
            await db.save_delivery(
                question_id=question_id, answer=notice, sources=[],
                status="unverified", delivered=False,
                delivery_status="rate_limited",
            )
            raise
        await db.save_delivery(
            question_id=question_id, answer=notice, sources=[],
            status="unverified", delivered=True,
            delivery_status="rate_limited",
        )
        return

    try:
        await message.bot.send_chat_action(
            chat_id=message.chat.id, action=ChatAction.TYPING
        )
    except Exception:
        logger.warning("Unable to send typing indicator", exc_info=True)
    search_error = False
    scope_blocked = blocked_request(question)
    try:
        if index is not None:
            await asyncio.to_thread(index.refresh)
        results = [] if scope_blocked else await asyncio.to_thread(
            retrieve_context, index, question,
            top_k=settings.top_k, min_score=settings.min_score,
        )
    except Exception:
        logger.exception("Retrieval failed")
        results = []
        search_error = True

    status = "no_evidence"
    if scope_blocked:
        answer = SCOPE_REFUSAL
        status = "unverified"
    elif results:
        try:
            answer = await llm.answer_question(question, results)
            status = "unverified"
            if answer == NO_EVIDENCE:
                status = "no_evidence"
            elif answer in {INVALID_ANSWER, EMPTY_ANSWER_MESSAGE} or not answer:
                status = "error"
        except Exception:
            logger.exception("LLM request failed")
            answer = LLM_ERROR_MESSAGE
            status = "error"
    elif search_error:
        answer = LLM_ERROR_MESSAGE
        status = "error"
    else:
        answer = NO_RESULTS_MESSAGE

    cited = {int(number) for number in re.findall(r"\[(\d+)\]", answer)}
    cited &= set(range(1, len(results) + 1))
    sources = [
        {
            "position": position,
            "document": chunk.document,
            "document_id": chunk.document_id or chunk.document,
            "page": chunk.page_label,
            "page_from": chunk.page_from,
            "locator": chunk.locator,
            "source_format": chunk.source_format,
            "score": round(score, 4),
        }
        for position, (score, chunk) in enumerate(results, start=1)
        if position in cited
    ]
    if status == "unverified" and sources:
        status = "with_sources"

    reply = _truncate(answer)
    source_links = _source_links(sources, settings)
    other_links: list[tuple[str, str]] = []
    offer_support = status == "no_evidence" and await db.clarification_count(
        telegram_id=message.from_user.id, chat_id=message.chat.id) >= 3
    if offer_support and settings.support_url:
        other_links.append(
            ("Связаться со специалистом", settings.support_url)
        )
    markup = (
        feedback_keyboard(
            question_id,
            other_links,
            has_sources=bool(source_links),
        )
        if question_id is not None or other_links
        else None
    )
    try:
        await message.answer(reply, reply_markup=markup)
    except Exception:
        await db.save_delivery(
            question_id=question_id, answer=answer, sources=sources,
            status=status, delivered=False,
        )
        raise
    await db.save_delivery(
        question_id=question_id, answer=answer, sources=sources,
        status=status, delivered=True,
    )
    if offer_support and settings.support_url:
        await db.record_escalation(question_id=question_id, status="offered")


@router.callback_query(F.data.startswith("sources:"))
async def handle_sources(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    try:
        parts = (callback.data or "").split(":")
        hiding = len(parts) == 3 and parts[1] == "hide"
        if not (len(parts) == 2 or hiding):
            raise ValueError("Invalid source callback")
        question_id = int(parts[-1])
        context = await db.get_question_context(
            question_id=question_id, telegram_id=callback.from_user.id
        )
    except (TypeError, ValueError):
        await callback.answer("Источники этого ответа недоступны.", show_alert=True)
        return
    message = callback.message
    if not isinstance(message, Message) or message.reply_markup is None:
        await callback.answer("Источники этого ответа недоступны.", show_alert=True)
        return
    links = _source_links(context["sources"], settings) if not hiding else []
    if not hiding and not links:
        await callback.answer("Ссылки на источники недоступны.", show_alert=True)
        return
    rows = []
    replaced = False
    for row in message.reply_markup.inline_keyboard:
        if hiding and all(
            button.url and button.text.startswith("Открыть источник [")
            for button in row
        ):
            continue
        if any(button.callback_data == callback.data for button in row):
            if hiding:
                rows.append([InlineKeyboardButton(
                    text="Источники",
                    callback_data=f"sources:{question_id}",
                )])
            else:
                rows.extend([
                    [InlineKeyboardButton(text=label, url=url)]
                    for label, url in links
                ])
                rows.append([InlineKeyboardButton(
                    text="Назад",
                    callback_data=f"sources:hide:{question_id}",
                )])
            replaced = True
        else:
            rows.append(row)
    if not replaced:
        await callback.answer("Кнопка уже обновлена.")
        return
    try:
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    except Exception:
        logger.exception("Could not reveal source links")
        await callback.answer("Не удалось показать источники.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("fb:"))
async def handle_feedback(callback: CallbackQuery, db: Database) -> None:
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
        await db.save_feedback(
            question_id=question_id,
            telegram_id=callback.from_user.id,
            value=value,
        )
    except Exception:
        logger.exception("Failed to save feedback")
        await callback.answer("Не удалось сохранить оценку.")
        return

    await callback.answer(FEEDBACK_MESSAGES[value])
    await _remove_answer_buttons(callback)


async def _remove_answer_buttons(
    callback: CallbackQuery, *, ticket_question_id: int | None = None
) -> None:
    message = callback.message
    if (
        message is None or not isinstance(message, Message)
        or message.reply_markup is None
    ):
        return
    rows = []
    for row in message.reply_markup.inline_keyboard:
        remaining = [
            button for button in row
            if not (button.callback_data or "").startswith(
                ("fb:", "resolution:", "ticket:create:")
            )
        ]
        if remaining:
            rows.append(remaining)
    if ticket_question_id is not None:
        rows.append([InlineKeyboardButton(
            text="Создать заявку",
            callback_data=f"ticket:create:{ticket_question_id}",
        )])
    try:
        markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        await message.edit_reply_markup(reply_markup=markup)
    except Exception:
        logger.exception("Could not remove answer buttons")


@router.callback_query(F.data.startswith("resolution:"))
async def handle_resolution(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    try:
        _, value, raw_id = (callback.data or "").split(":")
        question_id = int(raw_id)
        await db.save_resolution(
            question_id=question_id,
            telegram_id=callback.from_user.id,
            value=value,
        )
    except (ValueError, TypeError):
        await callback.answer("Не удалось сохранить ответ.")
        return
    response = "Спасибо, ответ сохранён."
    ticket_question_id = (
        question_id
        if value == "unresolved" and getattr(settings, "ticket_provider", "")
        else None
    )
    if ticket_question_id is not None:
        response = "Вопрос не решён. Нажмите «Создать заявку» под ответом."
    await callback.answer(response)
    await _remove_answer_buttons(callback, ticket_question_id=ticket_question_id)


@router.callback_query(F.data.startswith("ticket:create:"))
async def handle_ticket_create(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    provider = getattr(settings, "ticket_provider", "")
    if not provider:
        await callback.answer(
            "Создание заявок пока не настроено.", show_alert=True
        )
        return
    try:
        raw_id = (callback.data or "").split(":")
        if len(raw_id) != 3 or raw_id[:2] != ["ticket", "create"]:
            raise ValueError("Invalid ticket callback")
        question_id = int(raw_id[2])
        job, _ = await request_support_ticket(
            db,
            question_id=question_id,
            telegram_id=callback.from_user.id,
            provider=provider,
        )
    except (ValueError, TypeError):
        await callback.answer(
            "Не удалось создать заявку для этого вопроса.",
            show_alert=True,
        )
        return
    except Exception:
        logger.exception("Could not queue support ticket")
        await callback.answer(
            "Заявка пока недоступна. Повторите позже.", show_alert=True
        )
        return
    await callback.answer("Запрос принят.")
    if isinstance(callback.message, Message):
        await callback.message.answer(_ticket_message(job))


@router.message(F.text.startswith("/"))
async def handle_unknown_command(
    message: Message, index: SearchIndex, llm: LLMClient,
    db: Database, settings: Settings,
) -> None:
    if message.from_user and gibberish_mute.is_muted(message.from_user.id):
        return
    text = (message.text or "").strip()
    if re.fullmatch(
        r"/[a-zA-Z0-9_]{1,32}(?:@[a-zA-Z0-9_]{1,32})?(?:\s.*)?",
        text,
    ):
        await message.answer("Неизвестная команда. Доступны /start и /help.")
        return
    await handle_question(message, index, llm, db, settings)
