"""Create aggregate-only PostgreSQL views for the Grafana reader role."""

from __future__ import annotations

import asyncio
import os

import asyncpg


VIEWS = {
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
        SELECT date_trunc('day', created_at) AS day,
               operator_status AS status, count(*)::bigint AS total
        FROM analytics WHERE operator_status IS NOT NULL GROUP BY 1, 2
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
