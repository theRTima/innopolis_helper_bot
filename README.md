# Innopolis Helper Bot

Telegram assistant for internal documentation, with source citations, feedback,
and support escalation. Docling and a remote multimodal model prepare documents;
BM25 retrieval supplies evidence to an OpenAI-compatible answer model.

## Docker setup

Install Docker with Compose. The stack provides:

- `postgres` — database
- `bot` — Telegram polling process
- `viewer` — signed document viewer on loopback
- `ingest` — one-shot scanner that runs `ingest -> render -> extract` for every
  PDF in `data/knowledge`

Get a Telegram bot token from @BotFather and an image-capable model endpoint
for extraction. Start by creating the environment file:

```bash
cp .env.example .env
```

Set at least:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERNAMES=@alice,bob
LLM_BASE_URL=https://your-provider.example.com/v1
LLM_MODEL=your-multimodal-model-id
LLM_API_KEY=your-api-key
POSTGRES_PASSWORD=replace-with-a-strong-password
GRAFANA_ADMIN_PASSWORD=replace-with-another-password
GRAFANA_DB_PASSWORD=replace-with-a-reader-password
```

Put all PDFs into `data/knowledge/`. No per-file environment variables are
needed. Document IDs are derived from filenames, while existing stored manifests
keep their current IDs.

Build images, start PostgreSQL, and process documents before starting the bot:

```bash
mkdir -p data/processed
docker compose --profile ingest build bot viewer ingest
docker compose up -d --wait postgres
docker compose --profile ingest run --rm ingest
docker compose up -d bot viewer
```

The ingestion image installs CPU-only PyTorch to avoid unnecessary CUDA/NVIDIA
libraries. The bot requires at least one ready chunk set and applies migrations
at startup.

Add or replace PDFs later and run the same command again. Existing unchanged
files use the extraction cache; new or changed files are processed
automatically. The bot loads chunks at startup, so restart it after ingestion:

```bash
docker compose restart bot
```

To preview the derived document IDs without running extraction:

```bash
docker compose --profile ingest run --rm ingest --dry-run
```

The ingestion container writes to `./data/processed`; the bot reads that host
directory read-only. `./data/knowledge` is also mounted read-only. On Linux,
set `HOST_UID` and `HOST_GID` in `.env` to your user ID so generated files keep
the expected ownership.

If the model endpoint runs on the host, set:

```env
LLM_BASE_URL=http://host.docker.internal:8081/v1
```

Grafana is available at `http://localhost:3000/d/bot-analytics`. Set
`GRAFANA_ADMIN_PASSWORD` and `GRAFANA_DB_PASSWORD` in `.env`, then initialize
the aggregate views and their read-only database role once after migrations:

```bash
docker compose run --rm --no-deps bot alembic upgrade head
docker compose run --rm --no-deps bot python scripts/setup_grafana_reader.py
docker compose up -d grafana
```

Log in as `admin` using `GRAFANA_ADMIN_PASSWORD`. For escalation set
`SUPPORT_URL` to an HTTPS or Telegram link. After four consecutive clarification
requests the bot sends a button with that link; delivery indicates that Telegram
accepted the button, not that a specialist received the request. Older records
without explicit user feedback have an unknown outcome. Re-run the setup script
after upgrading the database to refresh the aggregate views.

The bot rate-limits individual questions to one every three seconds. Eight
messages from the same user within five seconds trigger a 30-second cooldown.
The rate-limit reply states the allowed interval and the remaining wait; the
cooldown reply states the message limit, the time window and the cooldown.
After the first cooldown warning, further messages during the cooldown are
logged but receive no reply. Limits are held in process memory, so they reset
on restart and are not shared between bot instances.

The ingestion image includes LibreOffice. The Docker scanner currently discovers
PDFs only; use the local pipeline below for other formats.

Logs and shutdown:

```bash
docker compose logs -f bot
docker compose down
```

## Telegram access

Set `TELEGRAM_ALLOWED_USERNAMES=@alice,bob` in `.env`. Entries are
comma-separated; `@` is optional and matching ignores case. Use Telegram
usernames, not display names. An empty list denies everyone, and users without
a username cannot access the bot. Messages, commands, and feedback callbacks
are checked before bot handlers run. Access follows the current username;
update the list when a user changes it.

Restart local runs after editing `.env`. Docker environment changes require
recreating the bot container:

```bash
docker compose up -d --force-recreate bot
```

## Local Python setup

- Python 3.11+
- A Telegram bot token from @BotFather
- An OpenAI-compatible model endpoint
- A multimodal model for PDF/image ingestion
- LibreOffice only for Office previews and Office extraction

Python dependencies are split into:

| File | Purpose |
| --- | --- |
| `requirements/base.txt` | Installs the project and all runtime dependencies from `pyproject.toml` |
| `requirements/ingestion.txt` | Docling, Jinja2, PyMuPDF |
| `requirements/dev.txt` | Test and lint dependencies |

### Install dependencies

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/base.txt
.venv/bin/python -m pip install -r requirements/ingestion.txt
```

### Configure `.env`

```bash
cp .env.example .env
```

Minimum configuration:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERNAMES=@alice,bob

LLM_BASE_URL=https://your-provider.example.com/v1
LLM_MODEL=your-multimodal-model-id
LLM_API_KEY=your-api-key

# Optional provider routing headers
LLM_SESSION_ID=innopolis-helper-bot
LLM_USER_AGENT=innopolis-helper-bot/1.0
```

The same model settings are used by the bot for answer generation and by
`app.rag.extract` for the normalized vision pipeline.

The examples use POSIX environment paths (`.venv/bin/python`). On Windows use
`.venv\Scripts\python.exe` with the same arguments.

The model must support **image input** for document extraction. A text-only
model can answer questions from existing chunks but cannot process new PDFs or
images.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_ALLOWED_USERNAMES` | Empty: deny everyone | Comma-separated username whitelist |
| `SUPPORT_URL` | Empty | Support button destination |
| `LLM_BASE_URL` | local fallback | OpenAI-compatible base URL, usually ending in `/v1` |
| `LLM_MODEL` | `qwen3.8-flash` | Provider model ID |
| `LLM_API_KEY` | `local` | Provider API key |
| `LLM_SESSION_ID` | `innopolis-helper-bot` | Stable routing/session header |
| `LLM_USER_AGENT` | `innopolis-helper-bot/1.0` | Client identification |
| `LLM_ANSWER_REASONING_EFFORT` | `minimal` | Fallback reasoning effort for answers when thinking budget is not used |
| `LLM_EXTRACTION_REASONING_EFFORT` | `low` | Reasoning effort for picture/page analysis and chunk grouping |
| `LLM_ENABLE_THINKING` | `auto` | Bounded thinking for the tested OpenCode/qwen pair |
| `LLM_THINKING_BUDGET` | provider default (`512`) | Maximum thinking tokens when enabled |
| `ANSWER_MAX_COMPLETION_TOKENS` | `1400` | Combined thinking + answer token budget |
| `ANSWER_MAX_TOKENS` | `600` | Answer generation limit |
| `TEMPERATURE` | `0.1` | Answer generation temperature |
| `TOP_K` | `2` | Initial retrieval seeds before bounded context expansion |
| `MIN_SCORE` | `0` | Minimum BM25 score |
| `KNOWLEDGE_DIR` | `data/knowledge` | Source PDF directory used for ingest and startup checks |
| `DOCUMENTS_DIR` | `data/processed/documents` | Versioned `app/rag` store |
| `DATABASE_PATH` | `data/bot.sqlite3` | SQLite path |
| `DATABASE_URL` | — | Optional PostgreSQL URL |
| `DOCUMENTS_BASE_URL` | — | Public viewer URL for signed source links |
| `DOCUMENT_LINK_SECRET` | — | HMAC secret for source links |

`LLM_SESSION_ID` and `LLM_USER_AGENT` are required by some providers (for
example, OpenCode Go uses `x-opencode-session` for routing). They are harmless
for generic OpenAI-compatible providers.

Reasoning levels are provider model parameters (`none`, `minimal`, `low`,
`medium`, `high`). Extraction uses `low`; answer generation uses a bounded
thinking budget for the tested OpenCode/qwen pair. For other providers/models,
`LLM_ANSWER_REASONING_EFFORT` is used when thinking is not explicitly enabled.
See `docs/answer-latency.md` for measurements and provider constraints.

### Prepare documentation

The project uses one document workflow: every source is stored as an immutable
version, rendered when needed, and extracted into versioned chunks.

The examples use `travel.pdf` and `furniture.pdf` as placeholders. Replace them
with the actual files under `data/knowledge/`.

#### Pipeline

```bash
# 1. Store an immutable version
.venv/bin/python -m app.rag.ingest \
  "data/knowledge/travel.pdf" \
  --document-id travel

# 2. Render a preview (optional for PDF, required for Office)
.venv/bin/python -m app.rag.render \
  "data/processed/documents/travel/sha256-<hash>" \
  --dpi 150

# 3. Extract retrieval chunks
.venv/bin/python -m app.rag.extract \
  "data/processed/documents/travel/sha256-<hash>"
```

Batch mode:

```bash
.venv/bin/python -m app.rag.render --all
.venv/bin/python -m app.rag.extract --all
```

#### Debug a single version

```bash
.venv/bin/python -m app.rag.extract \
  "data/processed/documents/travel/sha256-<hash>" \
  --pages 4,7
```

Debug extracts have `status: partial` and are ignored by the bot.

### Start the bot

```bash
.venv/bin/python -m app
```

Startup checks:

1. Telegram token is present.
2. Versioned chunks exist.
3. SQLite/PostgreSQL migrations are applied.
4. The LLM client is created.

If a PDF in `KNOWLEDGE_DIR` has no versioned chunks, it is skipped with a
warning and remains unavailable to the bot. At least one usable chunk set is
required for startup.

### Optional document viewer

Set both values in `.env`:

```env
DOCUMENTS_BASE_URL=http://127.0.0.1:8090
DOCUMENT_LINK_SECRET=some-long-random-secret
```

Start the viewer:

```bash
.venv/bin/python -m uvicorn app.anchors.viewer:app --host 127.0.0.1 --port 8090
```

The viewer serves signed source links for PDF and Office documents. The viewer reads originals from `KNOWLEDGE_DIR`. Remote users need a reachable
HTTPS URL, usually through a reverse proxy.

## Local administration preview

The optional FastAPI-Amis-Admin application displays questions, documents and
Telegram users. It has read-only model actions and a separate database role;
the `users` table contains Telegram IDs and creation dates, not names.

Install it in a separate environment: this admin version requires a different
FastAPI version from the one the bot image may install.

```bash
python3 -m venv storage/admin-venv
storage/admin-venv/bin/pip install -r requirements/admin.txt
```

Create the read-only PostgreSQL role using the bot database owner's URL and a
new password, then configure the panel with the reader role's URL:

```bash
DATABASE_URL='postgresql+asyncpg://bot:OWNER_PASSWORD@127.0.0.1:5438/bot' \
ADMIN_READER_PASSWORD='READER_PASSWORD' \
  storage/admin-venv/bin/python scripts/setup_admin_reader.py

export ADMIN_DATABASE_URL='postgresql+asyncpg://bot_admin_reader:READER_PASSWORD@127.0.0.1:5438/bot'
export ADMIN_USERNAME='admin'
export ADMIN_PASSWORD='CHOOSE_A_STRONG_PASSWORD'
storage/admin-venv/bin/uvicorn app.admin.web:create_app --factory --host 127.0.0.1 --port 8001
```

Replace the example passwords; percent-encode special characters in URLs. The
password for the panel is separate from the database password. Open
`http://127.0.0.1:8001/admin/` and enter the admin username and password.
The panel is a local preview using HTTP Basic authentication; configure HTTPS
and appropriate access control before exposing it beyond the machine. AMIS
loads interface assets from `unpkg.com` by default.

## Retrieval

- `TOP_K` defaults to `2` initial seeds, matching `fix/visual-rag-evidence`.
- BM25 uses the same search-only lexical hints as that branch. Evidence text
  is unchanged; titles and document names are not repeated to inflate scores.
- Table-of-contents chunks receive a score penalty for normal questions.
  Useful prose under a contents heading remains searchable.
- `SearchIndex.search` returns at most `top_k` matches; it does not append
  visual fragments outside that limit.
- The answer prompt explicitly tells the LLM to use diagram nodes/edges and
  table rows/columns when present.
- Context expansion (`app/rag/retrieve.py`) adds related chunks from the same
  document/version up to a bounded limit (6 chunks / 12,000 characters) and
  never truncates a chunk to fit.
- Schedule/contact questions use at most two seeds, even with a larger
  `TOP_K` override, so focused answers keep a small context.
- Offline diagnostics: `python -m app.rag.inspect` and
  `python -m app.rag.evaluate` (see `docs/visual-rag-plan.md`).

## Supported source formats

- PDF
- DOCX, XLSX, PPTX
- PNG, JPEG, WebP, TIFF, BMP
- text, Markdown, HTML, RST, AsciiDoc, TeX
- JSON, JSONL, XML, TOML, YAML, INI
- CSV, TSV
- source code and common project files

Office files require LibreOffice for rendering. For Office extraction, the
rendered PDF is used as the visual artifact; the original filename and version
stay in chunk metadata.

Install LibreOffice through your system package manager so that `soffice` is
available on `PATH`. If it is installed elsewhere, set `SOFFICE_PATH` or pass
`--soffice`.

## Tests

```bash
.venv/bin/python -m pip install -r requirements/dev.txt
.venv/bin/python -m pytest
```

Install ingestion dependencies before running the full suite. Offline tests
do not require model credentials.

## Troubleshooting

### `TELEGRAM_BOT_TOKEN is missing`

Create `.env` from `.env.example` and put the token from @BotFather into
`TELEGRAM_BOT_TOKEN`.

### `No chunks found`

Run the versioned pipeline:

```bash
.venv/bin/python -m app.rag.ingest "<source>" --document-id <id>
.venv/bin/python -m app.rag.render --all
.venv/bin/python -m app.rag.extract --all
```

### `Extraction is locked`

A previous extraction was interrupted. Check that no extraction process is
running, then remove:

```text
<version>/.extract.lock
```

The same applies to `.ingest.lock` and `.render.lock`.

### Model returns no JSON or empty chunks

- Confirm the model supports images.
- Check `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`.
- Some providers require `LLM_SESSION_ID`; the client sends it as
  `x-opencode-session`.
- Re-run extraction; the cache key changes when the model or prompts change.
- If the model still fails, extraction falls back to deterministic chunks for
  the affected elements.

### Office rendering fails

Install LibreOffice and ensure `soffice` is on `PATH`, or set `SOFFICE_PATH` /
pass `--soffice`. Rendering Office requires LibreOffice; extraction can then use
the rendered PDF.

### Access denied

Check the sender's current username against `TELEGRAM_ALLOWED_USERNAMES`.
Restart locally or recreate the Docker bot container after configuration changes.

Keep `.env`, internal documents, and generated artifacts out of commits.
