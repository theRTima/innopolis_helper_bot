"""Cheap request screening and fail-closed validation for documentation QA.

Patterns catch common requests, not every semantic jailbreak. The model must
also classify scope; valid JSON and citations do not prove factual grounding.
"""

import json
import re
import unicodedata

SCOPE_REFUSAL = (
    "Я помогаю только с вопросами по загруженной документации. "
    "Задайте вопрос о правилах, процессах или инструкциях из документов."
)
NO_EVIDENCE = (
    "В найденных фрагментах недостаточно сведений для достоверного ответа. "
    "Уточните вопрос или обратитесь к специалисту."
)
INVALID_ANSWER = (
    "Не удалось подтвердить ответ ссылками на найденные фрагменты. "
    "Уточните вопрос или обратитесь к специалисту."
)

BLOCKED_PATTERNS = tuple(
    re.compile(pattern, re.I | re.S)
    for pattern in (
        r"\b(ignore|forget|disregard|override)\b.{0,80}"
        r"\b(instructions?|rules?|prompts?|polic(?:y|ies)|system)\b",
        r"\b(игнорируй|забудь|отмени|обойди|не выполняй)\b.{0,80}"
        r"(инструкц|правил|промпт|ограничен)",
        r"\b(show|reveal|print|repeat|покажи|раскрой|выведи|повтори)\b"
        r".{0,60}(system\s+(prompt|message|instruction)|"
        r"системн\w*\s+(промпт|сообщен|инструкц)|api[_ -]?key)",
        r"\b(you are now|теперь ты)\b.{0,60}"
        r"(unrestricted|developer|dan\b|без ограничений|другой бот)",
        r"<\|(?:im_start|start_header_id|system|developer)"
        r"|\[INST\]|<<SYS>>",
        r"\b(write|compose|tell|generate|напиши|сочини|расскажи|придумай)"
        r"\b.{0,60}\b(joke\w*|poem\w*|song\w*|story|"
        r"анекдот\w*|стих\w*|песн\w*|шутк\w*)\b",
        r"\b(write|generate|напиши|сгенерируй)\b.{0,50}"
        r"\b(code|script|program|код|скрипт|программ\w*)\b",
    )
)


def blocked_request(question: str) -> bool:
    text = unicodedata.normalize("NFKC", question)
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    return any(pattern.search(text) for pattern in BLOCKED_PATTERNS)


def validated_answer(raw: str, source_count: int) -> str:
    """Never display raw, malformed or uncited model output."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return INVALID_ANSWER
    if not isinstance(payload, dict) or set(payload) != {"status", "answer"}:
        return INVALID_ANSWER
    status, answer = payload["status"], payload["answer"]
    if status == "out_of_scope":
        return SCOPE_REFUSAL
    if status == "no_evidence":
        return NO_EVIDENCE
    if status != "answer" or not isinstance(answer, str):
        return INVALID_ANSWER
    answer = answer.strip()
    references = re.findall(r"\[(\d+)\]", answer)
    if any(len(value) > 6 for value in references):
        return INVALID_ANSWER
    citations = [int(value) for value in references]
    if (
        not answer
        or len(answer) > 10000
        or not citations
        or any(not 1 <= value <= source_count for value in citations)
    ):
        return INVALID_ANSWER
    return answer
