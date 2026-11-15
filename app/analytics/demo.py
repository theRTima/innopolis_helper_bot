"""Fictional records for inspecting the dashboard without using a bot DB."""

from datetime import datetime, timedelta

from app.analytics.report import QuestionRecord


def example_records(now: datetime) -> list[QuestionRecord]:
    rows: list[QuestionRecord] = []

    def add(
        examples: list[str],
        *,
        age: int,
        area: str,
        kind: str,
        issue: str,
        unsure: int = 0,
        operator: bool = False,
    ) -> None:
        for position, example in enumerate(examples):
            missing = position < unsure
            feedback = "helpful" if position % 2 else None
            received_at = now - timedelta(days=age, minutes=position)
            document = {
                "bars_office": "БАРС-Офис.pdf",
                "business_trip": "БП Командировка.pdf",
                "furniture": "Реестр мебели.pdf",
            }.get(area)
            rows.append(
                QuestionRecord(
                    created_at=received_at,
                    user_id=len(rows) + 1,
                    area=area,
                    kind=kind,
                    issue=issue,
                    answer_status="no_evidence" if missing else "with_sources",
                    feedback="not_helpful" if missing else feedback,
                    escalation_status=(
                        "delivered" if operator and missing else None
                    ),
                    example=example,
                    resolution=(
                        "unresolved" if missing else
                        "resolved" if position % 2 else None
                    ),
                    delivery_status="delivered",
                    received_at=received_at,
                    sent_at=received_at + timedelta(seconds=8 + position),
                    documents=(document,) if document else (),
                )
            )

    calendar = {
        "area": "bars_office",
        "kind": "navigation",
        "issue": "Поиск производственного календаря",
    }
    add(
        [
            "Где найти производственный календарь?",
            "Как открыть календарь рабочих дней?",
            "Где посмотреть, какие дни рабочие?",
            "Не нахожу производственный календарь в меню.",
            "В каком разделе календарь праздников?",
            "Подскажите, где подробный календарь работы?",
        ],
        age=2,
        **calendar,
    )
    add(["Где посмотреть рабочие дни?"], age=10, **calendar)
    add(
        [
            "Почему не могу выбрать сделку в командировке?",
            "Куда пропала сделка из списка пресейлов?",
            "Не вижу нужную сделку при оформлении поездки.",
        ],
        age=3,
        area="business_trip",
        kind="conditions",
        issue="Выбор сделки для командировки",
        unsure=2,
        operator=True,
    )
    add(
        [
            "Как добавить стул?",
            "Как добавить стол?",
            "Как создать запись о мебели?",
        ],
        age=4,
        area="furniture",
        kind="instruction",
        issue="Добавление записи о мебели",
    )
    add(
        [
            "Где фильтр?",
            "Как отфильтровать по дате?",
            "Как открыть редактор фильтров?",
            "Как убрать фильтр?",
            "Где параметры фильтрации?",
        ],
        age=11,
        area="bars_office",
        kind="instruction",
        issue="Настройка фильтров",
    )
    return rows
