# Bot database

The database stores bot activity and the active, parsed document chunks. The
bot builds its in-memory BM25 index from active `DocumentVersion` rows and their
`Chunk` records. For documents without an active database version, it loads
chunks from the offline JSONL and versioned-file indexes in `CHUNKS_DIR` and
`DOCUMENTS_DIR`.
The ingestion CLI writes JSONL artifacts for inspection and imports complete
document parses into the relational tables.

By default, the bot uses asynchronous SQLite at `storage/bot.db`, overridden
by `DATABASE_PATH`. PostgreSQL remains available through `DATABASE_URL`. Alembic
applies migrations at startup. Existing `questions` and `feedback` rows are
preserved when the schema is initialized.

Each question is stored with its answer and cited sources. User, conversation
and message rows keep the interaction history; feedback is upserted by question
and Telegram user. Document versions are retained when a source changes, while
the bot retrieves from only the active version. `Chunk.text` is used for BM25;
`Chunk.content`, `title` and `data` preserve context for the answer model.

Install the runtime dependencies and inspect migrations with:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic current
```

Before moving an existing SQLite database, back it up. Its default location is
`storage/bot.db`; `DATABASE_URL` can be used to configure PostgreSQL instead.
