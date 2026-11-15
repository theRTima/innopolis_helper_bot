"""Create the limited role used by admin access, upload and deletion pages."""

import asyncio
import os

import asyncpg
from sqlalchemy.engine import make_url


async def main() -> None:
    url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://", "postgresql://", 1
    )
    access_url = os.environ["ADMIN_ACCESS_DATABASE_URL"]
    password = make_url(access_url).password
    if not password:
        raise ValueError("ADMIN_ACCESS_DATABASE_URL must contain a password")
    connection = await asyncpg.connect(url)
    try:
        async with connection.transaction():
            if not await connection.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = 'bot_admin_access'"
            ):
                await connection.execute("CREATE ROLE bot_admin_access LOGIN")
            quoted = password.replace("'", "''")
            await connection.execute(
                "ALTER ROLE bot_admin_access WITH LOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT "
                f"PASSWORD '{quoted}'"
            )
            await connection.execute(
                "ALTER ROLE bot_admin_access RESET default_transaction_read_only"
            )
            await connection.execute(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public "
                "FROM bot_admin_access"
            )
            await connection.execute(
                "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public "
                "FROM bot_admin_access"
            )
            await connection.execute(
                "GRANT USAGE ON SCHEMA public TO bot_admin_access"
            )
            await connection.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON allowed_telegram_users "
                "TO bot_admin_access"
            )
            await connection.execute(
                "GRANT SELECT, INSERT, UPDATE ON access_invites TO bot_admin_access"
            )
            await connection.execute(
                "GRANT SELECT ON access_invite_uses TO bot_admin_access"
            )
            await connection.execute(
                "GRANT SELECT, INSERT ON document_import_jobs "
                "TO bot_admin_access"
            )
            await connection.execute(
                "GRANT SELECT, DELETE ON documents, document_versions, "
                "import_runs, chunks TO bot_admin_access"
            )
            await connection.execute(
                "GRANT SELECT, DELETE ON citations TO bot_admin_access"
            )
            await connection.execute(
                "GRANT USAGE, SELECT ON SEQUENCE allowed_telegram_users_id_seq, "
                "access_invites_id_seq, document_import_jobs_id_seq TO bot_admin_access"
            )
        print("Limited admin access and document-upload role ready")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
