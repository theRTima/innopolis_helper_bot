"""Create aggregate-only PostgreSQL views for the Grafana reader role."""

from __future__ import annotations

import asyncio
import os

import asyncpg


VIEWS = {
    "grafana_activity_daily": """
        SELECT date_trunc('day', COALESCE(a.received_at, q.created_at::timestamp)) AS day,
               count(*)::bigint AS questions,
               count(DISTINCT q.telegram_id)::bigint AS users
        FROM questions q LEFT JOIN analytics a ON a.question_id = q.id GROUP BY 1
    """,
    "grafana_delivery_daily": """
        SELECT date_trunc('day', COALESCE(received_at, created_at)) AS day,
               COALESCE(delivery_status, 'unknown') AS status, count(*)::bigint AS total
        FROM analytics GROUP BY 1, 2
    """,
    "grafana_escalation_events_daily": """
        SELECT date_trunc('day', event_at) AS day, status,
               count(*)::bigint AS total
        FROM (
            -- Legacy delivered/failed events described a link, not a provider ticket.
            SELECT e.created_at AS event_at,
                   CASE e.status WHEN 'delivered' THEN 'legacy_link_sent'
                                 WHEN 'failed' THEN 'legacy_link_failed'
                                 ELSE e.status END AS status
            FROM escalation_events e
            WHERE e.status IN ('offered', 'requested')
               OR (e.status IN ('delivered', 'failed') AND NOT EXISTS (
                   SELECT 1 FROM ticket_jobs t
                   WHERE t.kind = 'support' AND t.question_id = e.question_id
               ))
            UNION ALL
            -- One confirmed creation per support job, dated by the worker event.
            SELECT COALESCE((
                       SELECT max(e.created_at) FROM escalation_events e
                       WHERE e.question_id = t.question_id
                         AND e.status = 'delivered'
                   ), t.created_at) AS event_at,
                   'created' AS status
            FROM ticket_jobs t
            WHERE t.kind = 'support' AND t.external_id IS NOT NULL
            UNION ALL
            SELECT COALESCE((
                       SELECT max(e.created_at) FROM escalation_events e
                       WHERE e.question_id = t.question_id
                         AND e.status = 'failed'
                   ), t.updated_at) AS event_at,
                   'failed' AS status
            FROM ticket_jobs t
            WHERE t.kind = 'support' AND t.state = 'failed'
        ) events GROUP BY 1, 2
    """,
    "grafana_import_details_daily": """
        SELECT date_trunc('day', created_at) AS day, parser, imported,
               count(*)::bigint AS runs, sum(chunk_count)::bigint AS chunks,
               sum(model_calls)::bigint AS model_calls, sum(cache_hits)::bigint AS cache_hits,
               avg(parse_ms)::double precision AS parse_ms,
               avg(text_ms)::double precision AS text_ms,
               avg(vision_ms)::double precision AS vision_ms,
               avg(total_ms)::double precision AS total_ms
        FROM import_runs GROUP BY 1, 2, 3
    """,
    "grafana_outcomes_daily": """
        SELECT date_trunc('day', COALESCE(a.received_at, q.created_at::timestamp)) AS day,
               CASE WHEN t.id IS NOT NULL
                              OR a.operator_status = 'requested'
                              OR qa.escalation_status = 'requested'
                    THEN 'unresolved' ELSE COALESCE(a.resolution, 'unknown') END AS status,
               count(*)::bigint AS total,
               count(*) FILTER (WHERE t.id IS NOT NULL
                                  OR a.operator_status = 'requested'
                                  OR qa.escalation_status = 'requested')::bigint AS escalated,
               sum(EXTRACT(EPOCH FROM (a.sent_at - a.received_at)))
                   FILTER (WHERE a.sent_at >= a.received_at) AS response_seconds,
               count(*) FILTER (WHERE a.sent_at >= a.received_at)::bigint AS response_count
        FROM questions q
        JOIN analytics a ON a.question_id = q.id
        LEFT JOIN question_analytics qa ON qa.question_id = q.id
        LEFT JOIN ticket_jobs t ON t.question_id = q.id AND t.kind = 'support'
        WHERE a.delivery_status = 'delivered'
          AND a.answer_status IN ('with_sources', 'no_evidence', 'unverified', 'error')
        GROUP BY 1, 2
    """,
    "grafana_answer_daily": """
        SELECT date_trunc('day', created_at) AS day,
               COALESCE(answer_status, 'pending') AS status,
               count(*)::bigint AS total
        FROM analytics GROUP BY 1, 2
    """,
    "grafana_feedback_daily": """
        SELECT date_trunc('day', created_at::timestamp) AS day,
               value AS status, count(*)::bigint AS total
        FROM feedback GROUP BY 1, 2
    """,
    "grafana_operator_daily": """
        SELECT day, status, total FROM grafana_escalation_events_daily
    """,
    "grafana_import_daily": """
        SELECT date_trunc('day', created_at) AS day,
               count(*)::bigint AS imports,
               sum(model_calls)::bigint AS model_calls,
               sum(cache_hits)::bigint AS cache_hits,
               round(avg(parse_ms))::bigint AS avg_parse_ms
        FROM import_runs GROUP BY 1
    """,
}


async def main() -> None:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    password = os.environ["GRAFANA_DB_PASSWORD"]
    if not password:
        raise ValueError("GRAFANA_DB_PASSWORD is empty")
    connection = await asyncpg.connect(url)
    try:
        async with connection.transaction():
            # Recreate views so schema changes (for example a widened column
            # type) do not fail on CREATE OR REPLACE VIEW.
            await connection.execute(
                "DROP VIEW IF EXISTS " + ", ".join(VIEWS) + " CASCADE"
            )
            for name, query in VIEWS.items():
                await connection.execute(f"CREATE OR REPLACE VIEW {name} AS {query}")
            exists = await connection.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader'"
            )
            if not exists:
                await connection.execute("CREATE ROLE grafana_reader LOGIN")
            quoted_password = password.replace("'", "''")
            await connection.execute(
                f"ALTER ROLE grafana_reader WITH PASSWORD '{quoted_password}'"
            )
            await connection.execute("GRANT USAGE ON SCHEMA public TO grafana_reader")
            await connection.execute(
                "GRANT SELECT ON " + ", ".join(VIEWS) + " TO grafana_reader"
            )
        print("Grafana aggregate views and read-only role ready")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
