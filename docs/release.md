# Release 0.1.0

The running bot, document viewer, and Grafana use the PostgreSQL `bot` database
from `docker-compose.yaml`. Only the containerized bot polls Telegram.

## Operations

```bash
docker compose ps
docker compose logs --tail=50 bot
docker compose logs --tail=50 grafana
docker compose up -d bot viewer grafana
```

Grafana is available at `http://127.0.0.1:3000/d/bot-analytics` from the host.
The admin account is `admin`; its password is `GRAFANA_ADMIN_PASSWORD` in the
local `.env`. The dashboard uses aggregate PostgreSQL views. Run
`scripts/setup_grafana_reader.py` after restoring a fresh PostgreSQL database
to recreate those views and the restricted reader role.

## Data cutover and rollback

The pre-cutover SQLite and PostgreSQL backups are in
`storage/backups/2026-11-15-release/` (ignored by Git). Both backups were
checked before migration. PostgreSQL was upgraded to Alembic revision
`e61ab0c8f492`, then the SQLite rows were merged with new IDs in one
transaction. The SQLite file remains intact as a fallback.

To fall back to SQLite, stop the container bot first, then start the local
bot from this repository with `DATABASE_URL` unset. Do not run both pollers
at once: Telegram rejects competing `getUpdates` requests.
