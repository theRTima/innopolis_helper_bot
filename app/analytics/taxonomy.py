"""Configurable product areas and reusable types of user questions."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_AREAS = {
    "bars_office": "БАРС-Офис",
    "business_trip": "Командировки",
    "furniture": "Реестр мебели",
    "account": "Учётная запись",
}
COMMON_AREAS = {"other": "Другая область", "unknown": "Область не определена"}
QUESTION_KINDS = {
    "navigation": "Навигация",
    "instruction": "Как выполнить действие",
    "access": "Доступ",
    "error": "Ошибка",
    "status": "Статус или согласование",
    "conditions": "Условия и ограничения",
    "feature_request": "Предложение улучшения",
    "information": "Справка",
    "other": "Другой вопрос",
    "unknown": "Тип не определён",
}

REVIEW_ACTIONS = {
    "navigation": "Проверьте, легко ли найти этот раздел в меню и справке.",
    "instruction": "Проверьте инструкцию и подсказки на этом шаге.",
    "access": "Проверьте порядок выдачи доступа и подсказки при входе.",
    "error": "Изучите примеры и попробуйте воспроизвести затруднение.",
    "status": "Проверьте, видны ли пользователю статус и следующий шаг.",
    "conditions": "Проверьте, где пользователю видны правила и исключения.",
    "feature_request": "Изучите, какую задачу пользователи хотят решить.",
}

_AREA_CODE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_CONTACT_DETAILS = re.compile(
    r"https?://\S+|[\w.+%-]+@[\w.-]+\.[a-zа-я]{2,}|"
    r"(?<!\w)@[\w_]+|(?<!\w)\+?\d[\d ()-]{7,}\d",
    re.IGNORECASE,
)


def load_areas() -> dict[str, str]:
    path = os.getenv("ANALYTICS_AREAS_FILE", "").strip()
    if not path:
        return DEFAULT_AREAS | COMMON_AREAS

    with Path(path).expanduser().open(encoding="utf-8-sig") as file:
        areas = json.load(file)
    if not isinstance(areas, dict) or not 1 <= len(areas) <= 40:
        raise ValueError("Укажите от 1 до 40 областей в JSON-объекте")
    if any(
        not _AREA_CODE.fullmatch(key)
        or key in COMMON_AREAS
        or not isinstance(label, str)
        or not 1 <= len(label.strip()) <= 80
        for key, label in areas.items()
    ):
        raise ValueError("Некорректный код или название области аналитики")
    return {key: label.strip() for key, label in areas.items()} | COMMON_AREAS


def redact_details(text: str) -> str:
    return " ".join(_CONTACT_DETAILS.sub("[скрыто]", text).split())
