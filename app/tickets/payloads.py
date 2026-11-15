"""Minimal, redacted text sent to an external ticket provider."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from app.analytics.taxonomy import redact_details
from app.tickets.providers import TicketPayload


def issue_key(area: str, kind: str, issue: str) -> str:
    canonical = "\x1f".join(
        part.strip().casefold() for part in (area, kind, issue)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe(value: object, limit: int) -> str:
    return redact_details(str(value or ""))[:limit].strip()


def support_payload(context: Mapping[str, object]) -> TicketPayload:
    """Build a support request without identity or raw contact data."""
    question_id = int(context["question_id"])
    question = _safe(context.get("question"), 1200)
    answer = _safe(context.get("answer"), 1800)
    raw_sources = context.get("sources") or []
    if isinstance(raw_sources, str):
        try:
            raw_sources = json.loads(raw_sources)
        except ValueError:
            raw_sources = []
    sources = []
    if isinstance(raw_sources, list):
        for source in raw_sources[:5]:
            if not isinstance(source, dict):
                continue
            document = _safe(source.get("document"), 160)
            page = _safe(source.get("page"), 30)
            if document:
                sources.append(
                    f"{document}, стр. {page}" if page else document
                )

    messages = context.get("messages") or []
    history = []
    if isinstance(messages, Sequence) and not isinstance(
        messages, (str, bytes)
    ):
        for message in messages[-8:]:
            if not isinstance(message, Mapping):
                continue
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            body = _safe(message.get("body"), 600)
            if body:
                history.append(f"{role}: {body}")

    description = [
        f"Локальный номер вопроса: {question_id}",
        f"Вопрос: {question}",
        f"Ответ бота: {answer or 'Ответ ещё не получен'}",
        "Источники: " + ("; ".join(sources) if sources else "нет"),
    ]
    if history:
        description.append("Последние сообщения:\n" + "\n".join(history))
    return TicketPayload(
        title=(
            f"Поддержка по вопросу #{question_id}: "
            f"{question[:90] or 'Запрос оператора'}"
        ),
        description="\n\n".join(description)[:7000],
        kind="support",
        local_key=f"support:{question_id}",
    )


def improvement_payload(
    *,
    area: str,
    kind: str,
    issue: str,
    period: str,
    questions: int,
    users: int,
    previous: int,
    unresolved: int,
    no_evidence: int,
    examples: Sequence[str],
    documents: Sequence[str],
    hypothesis: str,
) -> TicketPayload:
    """Keep observed data separate from a proposed explanation."""
    key = issue_key(area, kind, issue)
    facts = [
        f"Область: {_safe(area, 80)}",
        f"Тип: {_safe(kind, 80)}",
        f"Проблема: {_safe(issue, 180)}",
        f"Период: {_safe(period, 100)}",
        f"Вопросов: {questions}; пользователей: {users}",
        f"В предыдущем периоде: {previous}",
        (
            f"Не решено: {unresolved}; "
            f"без подтверждённого источника: {no_evidence}"
        ),
    ]
    clean_examples = [_safe(example, 250) for example in examples[:3]]
    clean_documents = [_safe(document, 180) for document in documents[:5]]
    if clean_examples:
        facts.append(
            "Примеры:\n" + "\n".join(f"- {item}" for item in clean_examples)
        )
    if clean_documents:
        facts.append("Документы: " + "; ".join(clean_documents))
    description = (
        "Наблюдения\n" + "\n".join(facts)
        + "\n\nГипотеза для проверки\n" + _safe(hypothesis, 1000)
    )
    return TicketPayload(
        title=f"Проверить проблему: {_safe(issue, 150)}",
        description=description[:7000],
        kind="improvement",
        local_key=f"improvement:{key}",
    )
