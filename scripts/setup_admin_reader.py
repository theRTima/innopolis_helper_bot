"""Prepare a PostgreSQL role that can only read the admin's three tables."""

import asyncio
import os

import asyncpg


async def main() -> None:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    password = os.environ["ADMIN_READER_PASSWORD"]
    if not password:
        raise ValueError("ADMIN_READER_PASSWORD is empty")
    connection = await asyncpg.connect(url)
    try:
        async with connection.transaction():
            if not await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'bot_admin_reader'"):
                await connection.execute("CREATE ROLE bot_admin_reader LOGIN")
            quoted = password.replace("'", "''")
            await connection.execute(
                f"ALTER ROLE bot_admin_reader WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD '{quoted}'"
            )
            await connection.execute("ALTER ROLE bot_admin_reader SET default_transaction_read_only = on")
            await connection.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM bot_admin_reader")
            await connection.execute("GRANT USAGE ON SCHEMA public TO bot_admin_reader")
            await connection.execute("GRANT SELECT ON questions, documents, users TO bot_admin_reader")
        print("Read-only admin role ready")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
