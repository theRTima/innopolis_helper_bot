"""Turn stored questions into counts and review suggestions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, null, select

from app.analytics.taxonomy import REVIEW_ACTIONS, redact_details
from app.storage.db import Database
from app.storage.models import Answer, Feedback, Question, QuestionAnalytics


@dataclass(frozen=True)
class QuestionRecord:
    created_at: datetime
    user_id: int
    area: str | None = None
    kind: str | None = None
    issue: str | None = None
    answer_status: str | None = None
    feedback: str | None = None
    escalation_status: str | None = None
    example: str | None = None


@dataclass(frozen=True)
class Counts:
    total: int
    users: int
    classified: int
    with_sources: int
    no_evidence: int
    unverified: int
    errors: int
    unknown_status: int
    rated: int
    helpful: int
    escalations: int
    delivered: int
    failed: int


@dataclass(frozen=True)
class Topic:
    area: str | None
    kind: str | None
    current: Counts
    previous: int


@dataclass(frozen=True)
class Signal:
    area: str
    kind: str
    title: str
    current: Counts
    previous: int
    rising: bool
    action: str
    examples: tuple[str, ...]


@dataclass(frozen=True)
class Report:
    start: datetime
    end: datetime
    current: Counts
    previous: Counts
    topics: list[Topic]
    signals: list[Signal]
    can_compare_trends: bool


def _counts(records: list[QuestionRecord]) -> Counts:
    with_sources = sum(row.answer_status == "with_sources" for row in records)
    no_evidence = sum(row.answer_status == "no_evidence" for row in records)
    unverified = sum(row.answer_status == "unverified" for row in records)
    errors = sum(row.answer_status == "error" for row in records)
    known_status = with_sources + no_evidence + unverified + errors
    return Counts(
        total=len(records),
        users=len({row.user_id for row in records}),
        classified=sum(row.area is not None for row in records),
        with_sources=with_sources,
        no_evidence=no_evidence,
        unverified=unverified,
        errors=errors,
        unknown_status=len(records) - known_status,
        rated=sum(
            row.feedback in {"helpful", "not_helpful"} for row in records
        ),
        helpful=sum(row.feedback == "helpful" for row in records),
        escalations=sum(row.escalation_status is not None for row in records),
        delivered=sum(row.escalation_status == "delivered" for row in records),
        failed=sum(row.escalation_status == "failed" for row in records),
    )


async def load_records(
    db: Database, *, since: datetime, until: datetime, show_examples: bool
) -> list[QuestionRecord]:
    analytics = QuestionAnalytics
    # Keep the date index usable and allow offset timestamps from PostgreSQL.
    first_date = (since.date() - timedelta(days=1)).isoformat()
    last_date = (until.date() + timedelta(days=2)).isoformat()
    stmt = (
        select(
            Question.created_at,
            Question.telegram_id,
            analytics.area,
            analytics.kind,
            analytics.issue,
            Answer.status,
            Feedback.value,
            analytics.escalation_status,
            Question.question if show_examples else null(),
        )
        .outerjoin(analytics, analytics.question_id == Question.id)
        .outerjoin(Answer, Answer.question_id == Question.id)
        .outerjoin(
            Feedback,
            and_(
                Feedback.question_id == Question.id,
                Feedback.telegram_id == Question.telegram_id,
            ),
        )
        .where(
            Question.created_at >= first_date, Question.created_at < last_date
        )
        .order_by(Question.id.desc())
    )
    async with db.session() as session:
        rows = (await session.execute(stmt)).all()

    records = []
    for timestamp, *data in rows:
        try:
            created_at = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError):
            continue
        created_at = (
            created_at.replace(tzinfo=UTC)
            if created_at.tzinfo is None
            else created_at.astimezone(UTC)
        )
        if since <= created_at < until:
            records.append(QuestionRecord(created_at, *data))
    return records


def build_report(
    records: list[QuestionRecord], *, days: int, end: datetime
) -> Report:
    start = end - timedelta(days=days)
    previous_start = start - timedelta(days=days)
    current = [row for row in records if start <= row.created_at < end]
    previous = [
        row for row in records if previous_start <= row.created_at < start
    ]
    totals = _counts(current)
    earlier = _counts(previous)

    current_topics, current_issues = _group(current)
    previous_topics, previous_issues = _group(previous)
    topics = [
        Topic(
            area,
            kind,
            _counts(rows),
            len(previous_topics.get((area, kind), [])),
        )
        for (area, kind), rows in current_topics.items()
    ]
    topics.sort(
        key=lambda topic: (
            -topic.current.total, topic.area or "", topic.kind or ""
        )
    )

    compare = (
        totals.total >= 5
        and earlier.total >= 5
        and totals.classified == totals.total
        and earlier.classified == earlier.total
    )
    signals = []
    for key, rows in current_issues.items():
        area, kind, title = key
        count = _counts(rows)
        if count.total < 3 or count.users < 2:
            continue
        older = len(previous_issues.get(key, []))
        rising = (
            compare
            and count.total - older >= 2
            and count.total / totals.total - older / earlier.total >= 0.05
        )
        action = REVIEW_ACTIONS.get(kind, "Изучите примеры похожих вопросов.")
        if count.no_evidence + count.unverified >= 2:
            action += " Проверьте, находится ли нужный ответ в базе знаний."
        examples = tuple(
            dict.fromkeys(
                redact_details(row.example)[:180]
                for row in rows
                if row.example
            )
        )[:2]
        signals.append(
            Signal(area, kind, title, count, older, rising, action, examples)
        )
    signals.sort(
        key=lambda signal: (
            not signal.rising, -signal.current.total, signal.title
        )
    )
    return Report(start, end, totals, earlier, topics, signals[:6], compare)


def _group(
    records: list[QuestionRecord],
) -> tuple[
    dict[tuple[str | None, str | None], list[QuestionRecord]],
    dict[tuple[str, str, str], list[QuestionRecord]],
]:
    topics = defaultdict(list)
    issues = defaultdict(list)
    for row in records:
        topics[row.area, row.kind].append(row)
        if row.area and row.kind and row.issue:
            issues[row.area, row.kind, row.issue].append(row)
    return topics, issues
