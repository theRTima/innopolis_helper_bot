"""Turn stored questions into counts and review suggestions."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, null, select

from app.analytics.taxonomy import REVIEW_ACTIONS, redact_details
from app.storage.db import Database
from app.storage.models import (
    Analitics, Answer, Feedback, Question, QuestionAnalytics, TicketJob,
)


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
    resolution: str | None = None
    delivery_status: str | None = None
    received_at: datetime | None = None
    sent_at: datetime | None = None
    documents: tuple[str, ...] = ()
    question_id: int | None = None


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
    eligible: int
    confirmed_resolved: int
    unresolved: int
    unknown_resolution: int
    failed_delivery: int
    suppressed: int
    response_seconds: float
    response_count: int


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
    documents: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        return self.area, self.kind, self.title


@dataclass(frozen=True)
class Report:
    start: datetime
    end: datetime
    current: Counts
    previous: Counts
    topics: list[Topic]
    signals: list[Signal]
    can_compare_trends: bool


@dataclass(frozen=True)
class IssueTaskRecord:
    area: str
    kind: str
    title: str
    external_id: str | None
    url: str | None
    status: str
    closed_at: datetime | None = None


@dataclass(frozen=True)
class TaskView:
    external_id: str | None
    url: str | None
    status: str
    closed_at: datetime | None
    before_total: int | None
    after_total: int | None
    before_unresolved: int | None
    after_unresolved: int | None


def _counts(records: list[QuestionRecord]) -> Counts:
    requested_statuses = {"requested", "delivered", "failed"}
    eligible_rows = [
        row for row in records
        if row.delivery_status == "delivered"
        and row.answer_status in {
            "with_sources", "no_evidence", "unverified", "error"
        }
    ]
    resolved = sum(
        row.resolution == "resolved"
        and row.escalation_status not in requested_statuses
        for row in eligible_rows
    )
    unresolved = sum(
        row.resolution == "unresolved"
        or row.escalation_status in requested_statuses
        for row in eligible_rows
    )
    durations = [
        (row.sent_at - row.received_at).total_seconds()
        for row in eligible_rows
        if row.sent_at is not None and row.received_at is not None
        and row.sent_at >= row.received_at
    ]
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
        escalations=sum(
            row.escalation_status in requested_statuses for row in records
        ),
        delivered=sum(row.escalation_status == "delivered" for row in records),
        failed=sum(row.escalation_status == "failed" for row in records),
        eligible=len(eligible_rows),
        confirmed_resolved=resolved,
        unresolved=unresolved,
        unknown_resolution=len(eligible_rows) - resolved - unresolved,
        failed_delivery=sum(
            row.delivery_status == "failed" for row in records
        ),
        suppressed=sum(
            row.delivery_status in {"suppressed", "rate_limited"}
            for row in records
        ),
        response_seconds=sum(durations),
        response_count=len(durations),
    )


def _as_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = (
            value if isinstance(value, datetime)
            else datetime.fromisoformat(value)
        )
    except (TypeError, ValueError):
        return None
    return (
        parsed.replace(tzinfo=UTC)
        if parsed.tzinfo is None else parsed.astimezone(UTC)
    )


def _document_names(raw: str | None) -> tuple[str, ...]:
    try:
        sources = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(sources, list):
        return ()
    return tuple(dict.fromkeys(
        redact_details(name)[:120]
        for item in sources
        if isinstance(item, dict)
        if isinstance(name := item.get("document"), str) and name.strip()
    ))


async def load_records(
    db: Database, *, since: datetime, until: datetime, show_examples: bool
) -> list[QuestionRecord]:
    analytics = QuestionAnalytics
    # Keep the date index usable and allow offset timestamps from PostgreSQL.
    first_date = (since.date() - timedelta(days=1)).isoformat()
    last_date = (until.date() + timedelta(days=2)).isoformat()
    stmt = (
        select(
            Question.id,
            Question.created_at,
            Question.telegram_id,
            analytics.area,
            analytics.kind,
            analytics.issue,
            Analitics.answer_status,
            Answer.status,
            Feedback.value,
            Analitics.operator_status,
            Question.question if show_examples else null(),
            Analitics.resolution,
            Analitics.delivery_status,
            Analitics.received_at,
            Analitics.sent_at,
            Question.sources,
            analytics.escalation_status,
            TicketJob.id,
            TicketJob.external_id,
            TicketJob.state,
        )
        .outerjoin(analytics, analytics.question_id == Question.id)
        .outerjoin(Analitics, Analitics.question_id == Question.id)
        .outerjoin(Answer, Answer.question_id == Question.id)
        .outerjoin(
            Feedback,
            and_(
                Feedback.question_id == Question.id,
                Feedback.telegram_id == Question.telegram_id,
            ),
        )
        .outerjoin(
            TicketJob,
            and_(
                TicketJob.question_id == Question.id,
                TicketJob.kind == "support",
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
    for (
        question_id, timestamp, user_id, area, kind, issue, analytics_status,
        answer_status, feedback, operator_status, example, resolution,
        delivery_status, received_at, sent_at, sources, old_escalation,
        ticket_id, external_id, ticket_state,
    ) in rows:
        created_at = _as_utc(timestamp)
        if created_at is None:
            continue
        if since <= created_at < until:
            if ticket_id is not None:
                escalation = (
                    "delivered" if external_id
                    else "failed" if ticket_state == "failed"
                    else "requested"
                )
            elif "requested" in (operator_status, old_escalation):
                escalation = "requested"
            elif operator_status or old_escalation:
                # Historic delivery events were support links, not tickets.
                escalation = "offered"
            else:
                escalation = None
            records.append(QuestionRecord(
                created_at=created_at,
                user_id=user_id,
                area=area,
                kind=kind,
                issue=issue,
                answer_status=analytics_status or answer_status,
                feedback=feedback,
                escalation_status=escalation,
                example=example,
                resolution=resolution,
                delivery_status=delivery_status,
                received_at=_as_utc(received_at),
                sent_at=_as_utc(sent_at),
                documents=_document_names(sources),
                question_id=question_id,
            ))
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
        documents = tuple(dict.fromkeys(
            document for row in rows for document in row.documents
        ))[:3]
        signals.append(
            Signal(area, kind, title, count, older, rising, action, examples,
                   documents)
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


def build_task_links(
    records: list[QuestionRecord],
    tasks: list[IssueTaskRecord],
    *,
    days: int,
    end: datetime,
    data_start: datetime,
) -> dict[tuple[str, str, str], TaskView]:
    """Compare complete, equally long periods around a task's closure."""
    links = {}
    duration = timedelta(days=days)
    for task in tasks:
        before_total = after_total = None
        before_unresolved = after_unresolved = None
        closed_at = _as_utc(task.closed_at)
        if (
            closed_at is not None
            and data_start <= closed_at - duration
            and end >= closed_at + duration
        ):
            related = [
                row for row in records
                if (row.area, row.kind, row.issue) == (
                    task.area, task.kind, task.title
                )
            ]
            before = _counts([
                row for row in related
                if closed_at - duration <= row.created_at < closed_at
            ])
            after = _counts([
                row for row in related
                if closed_at <= row.created_at < closed_at + duration
            ])
            before_total, after_total = before.total, after.total
            before_unresolved, after_unresolved = (
                before.unresolved, after.unresolved
            )
        links[task.area, task.kind, task.title] = TaskView(
            external_id=task.external_id,
            url=task.url,
            status=task.status,
            closed_at=closed_at,
            before_total=before_total,
            after_total=after_total,
            before_unresolved=before_unresolved,
            after_unresolved=after_unresolved,
        )
    return links


async def load_issue_tasks(db: Database) -> list[IssueTaskRecord]:
    """Read only task metadata; never pull provider payloads into the view."""
    statement = (
        select(
            TicketJob.issue_area,
            TicketJob.issue_kind,
            TicketJob.issue_title,
            TicketJob.external_id,
            TicketJob.external_url,
            TicketJob.external_status,
            TicketJob.state,
            TicketJob.closed_at,
        )
        .where(TicketJob.kind == "improvement")
        .order_by(TicketJob.id.desc())
    )
    async with db.session() as session:
        rows = (await session.execute(statement)).all()
    return [
        IssueTaskRecord(area, kind, title, external_id,
                        url if url and url.startswith(("https://", "http://"))
                        else None,
                        external_status or state, closed_at)
        for area, kind, title, external_id, url, external_status, state,
        closed_at in rows
        if area and kind and title
    ]
