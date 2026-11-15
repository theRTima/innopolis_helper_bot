# Bot database

The database stores bot activity, not the document search index. The index is
built from versioned JSONL chunks in `DOCUMENTS_DIR`; parsing and chunk metadata
do not require a database.

By default, the bot uses asynchronous SQLite at `data/bot.sqlite3`, overridden
by `DATABASE_PATH`. PostgreSQL remains available through `DATABASE_URL`. Alembic
applies migrations at startup. Existing `questions` and `feedback` rows are
preserved when the schema is initialized.

Each question is stored with its answer and cited sources. User, conversation
and message rows keep the interaction history; feedback is upserted by question
and Telegram user. The document/version/chunk/citation tables in the initial
schema are scaffolding and are not used by the current JSONL retrieval pipeline.

Install the runtime dependencies and inspect migrations with:

```bash
.venv/bin/python -m pip install -r requirements/base.txt
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

Before moving an existing SQLite database, back it up. Its default location is
`data/bot.sqlite3`; `DATABASE_URL` can be used to configure PostgreSQL instead.
