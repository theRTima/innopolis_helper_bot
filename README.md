# Innopolis Helper Bot

Telegram assistant for internal documentation, with source citations, feedback,
and support escalation. Docling and a remote multimodal model prepare documents;
BM25 retrieval supplies evidence to an OpenAI-compatible answer model.

## Docker setup

### What runs

| Service | Purpose | Address |
| --- | --- | --- |
| `bot` | Telegram polling process | — |
| `postgres` | PostgreSQL database | `127.0.0.1:5438` |
| `viewer` | Signed document viewer | `http://127.0.0.1:8090` |
| `grafana` | Analytics dashboards | `http://127.0.0.1:3000/d/bot-analytics` |
| `ingest` | One-shot PDF processing; not a long-running service | — |
| `admin` | Optional read-only admin panel | `http://127.0.0.1:8001/admin/` |
| `llama` | Optional local llama.cpp server | `http://127.0.0.1:8080` |

Published ports bind to `127.0.0.1` only, so the stack is not exposed to the
network by default.

### First-time launch

Follow these steps once on a new machine.

**1. Install Docker with Compose.**

**2. Create the environment file.**

```bash
cp .env.example .env
```

Set at least these values in `.env`:

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

`TELEGRAM_ALLOWED_USERNAMES` is required for access. An empty list denies
everyone. Use Telegram usernames, not display names.

**3. Add the source PDFs.**

```bash
mkdir -p data/knowledge data/processed
```

Copy the PDFs into `data/knowledge/`. No per-file environment variables are
needed. Document IDs are derived from filenames, while existing stored manifests
keep their current IDs.

**4. Build the images.**

```bash
docker compose --profile ingest build bot viewer ingest
```

The ingestion image installs CPU-only PyTorch and LibreOffice, so the first
build takes several minutes and downloads a large amount of data.

**5. Start PostgreSQL and apply migrations.**

```bash
docker compose up -d --wait postgres
docker compose run --rm --no-deps bot alembic upgrade head
```

**6. Process every PDF.**

```bash
docker compose --profile ingest run --rm ingest
```

This runs `ingest -> render -> extract` for all PDFs in `data/knowledge/`.
Extraction calls the configured multimodal model and can take a long time for
large documents. Unchanged files are cached on later runs.

**7. Initialize Grafana access.**

```bash
docker compose run --rm --no-deps bot python scripts/setup_grafana_reader.py
```

This creates the aggregate views and the read-only `grafana_reader` role.

**8. Start the long-running services.**

```bash
docker compose up -d bot viewer grafana
```

**9. Verify the launch.**

```bash
docker compose ps
docker compose logs --tail=50 bot
```

`postgres` should be `healthy`, and the bot log should contain a line like:

```text
Run polling for bot @your_bot_name
```

Open:

- Grafana: `http://localhost:3000/d/bot-analytics`
- Viewer: `http://localhost:8090`

Log in to Grafana as `admin` with `GRAFANA_ADMIN_PASSWORD` from `.env`.

If the bot reports that no chunks were found, repeat step 6. At least one
successfully extracted document is required for startup.

On Linux, set `HOST_UID` and `HOST_GID` in `.env` to your user ID so the
ingestion container writes files with the expected ownership.

If the model endpoint runs on the host, set:

```env
LLM_BASE_URL=http://host.docker.internal:8081/v1
```

### Add or replace PDFs later

The bot does **not** need to be stopped while ingestion runs. The ingestion
container reads `data/knowledge/` and writes `data/processed/`; the bot reads
the processed directory read-only.

```bash
cp new-document.pdf data/knowledge/
docker compose --profile ingest run --rm ingest
docker compose restart bot
```

The scanner discovers all PDFs automatically. Existing unchanged files use the
extraction cache; new or changed files are processed. The bot loads chunks at
startup, so **restart it after ingestion** to make the new documents available.

To preview the derived document IDs without running extraction:

```bash
docker compose --profile ingest run --rm ingest --dry-run
```

If you prefer not to serve questions during a long extraction, stop the bot
first and start it afterwards:

```bash
docker compose stop bot
docker compose --profile ingest run --rm ingest
docker compose start bot
```

### Start, stop and restart services

Start or stop the whole stack while keeping containers and data:

```bash
docker compose stop
docker compose start
```

Remove the containers and network but keep the PostgreSQL and Grafana data
volumes:

```bash
docker compose down
```

Remove the containers **and delete all stored data** (database, analytics and
Grafana state). This is destructive:

```bash
docker compose down -v
```

Restart one service:

```bash
docker compose restart bot
docker compose restart viewer
docker compose restart grafana
docker compose restart postgres
```

Restarting PostgreSQL can briefly interrupt the bot, viewer and Grafana. They
usually reconnect automatically; if not, restart them as well.

`docker compose restart` does **not** reload `.env`. After changing environment
variables, recreate the affected container:

```bash
docker compose up -d --force-recreate bot
docker compose up -d --force-recreate grafana
```

Stop or start an individual service without recreating it:

```bash
docker compose stop bot
docker compose start bot
```

`ingest` is a one-shot job, not a daemon. Run it with
`docker compose --profile ingest run --rm ingest` whenever documents change.

Useful diagnostics:

```bash
docker compose ps
docker compose logs -f bot
docker compose logs -f grafana
docker compose logs -f postgres
```

### Grafana

Grafana is available at `http://localhost:3000/d/bot-analytics`. Set
`GRAFANA_ADMIN_PASSWORD` and `GRAFANA_DB_PASSWORD` in `.env`, then run the
initialization command from the first-time launch if you have not done so
already:

```bash
docker compose run --rm --no-deps bot python scripts/setup_grafana_reader.py
docker compose up -d grafana
```

If you change `GRAFANA_DB_PASSWORD`, rerun the setup script and recreate
Grafana:

```bash
docker compose run --rm --no-deps bot python scripts/setup_grafana_reader.py
docker compose up -d --force-recreate grafana
```

Re-run the setup script after a database upgrade to refresh the aggregate
views. `SUPPORT_URL` offers a support link after repeated unanswered questions;
showing the link is recorded separately from requesting support. A ticket is
marked delivered only after the selected ticket system confirms creation and
returns an external ID. Older records without explicit feedback retain an
unknown outcome.

### Rate limits

The bot allows one question every three seconds. Eight messages from the same
user within five seconds trigger a 30-second cooldown. The rate-limit reply
states the allowed interval and the remaining wait; the cooldown reply states
the message limit, the time window and the cooldown. After the first cooldown
warning, further messages during the cooldown are logged but receive no reply.
Limits are held in process memory, so they reset on restart and are not shared
between bot instances.

### Ingestion notes

The ingestion image includes LibreOffice. The Docker scanner currently discovers
PDFs only; use the local pipeline below for other formats.

## Telegram access

Set `TELEGRAM_ALLOWED_USERNAMES=@alice,bob` in `.env`. Entries are
comma-separated; `@` is optional and matching ignores case. A repeated `@`
(such as `@@alice`) is tolerated. Use Telegram usernames, not display names. An
empty list denies everyone, and users without a username cannot access the bot.
Messages, commands, and feedback callbacks are checked before bot handlers run.
Access follows the current username; update the list when a user changes it.

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
| `ANALYTICS_PASSWORD` | — | Password for the internal analytics panel |
| `TICKET_PROVIDER` | Empty: disabled | `demo`, `jsm`, `zendesk`, or `usedesk` |
| `TICKET_BASE_URL` | — | Real ticket provider API base URL |
| `TICKET_API_TOKEN` | — | Real provider secret; never commit it |

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

## Support analytics

The analytics dashboard groups related questions and shows recurring problems,
answer outcomes, and changes between periods. A separate worker labels new
questions without delaying Telegram replies. A reviewer can confirm one
improvement task from a recurring problem; personal support requests use a
durable background queue. Run the optional services with:

```bash
docker compose --profile analytics up -d analytics analytics-labeler
docker compose --profile tickets up -d ticket-worker
```

Set `ANALYTICS_PASSWORD` and `TICKET_PROVIDER=demo` in `.env` for an offline
trial. The analytics panel listens on `http://127.0.0.1:8002/`. To see both
flows without Telegram or an external API, run `python scripts/demo_ticket_flow.py`.
Jira Service Management, Zendesk and Usedesk adapters have mock contract tests;
they still need credentials and validation against the team's test project.
See [analytics and ticket setup](docs/analytics.md).

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
`http://127.0.0.1:8001/admin/`; the panel redirects to its English sign-in page.
Successful sign-in creates an eight-hour, HTTP-only session cookie. Keep the
panel bound to localhost, or configure HTTPS and suitable access controls before
exposing it beyond the machine. AMIS loads interface assets from `unpkg.com` by
default.

### Run the administration panel in Docker

Prepare the read-only `bot_admin_reader` role as shown above. Put the following
values in the ignored file `storage/admin.env` (create the directory if needed):

```env
ADMIN_DATABASE_URL=postgresql+asyncpg://bot_admin_reader:READER_PASSWORD@127.0.0.1:5438/bot
ADMIN_USERNAME=admin
ADMIN_PASSWORD=CHOOSE_A_STRONG_PASSWORD
```

Replace the example passwords and restrict the file to its owner, for example
with `chmod 600 storage/admin.env`. Docker Compose points this URL at the
`postgres` service inside its network; the same file also works for the local
preview. Build and start the optional service:

```bash
docker compose --profile admin up -d --build admin
docker compose --profile admin logs -f admin
docker compose --profile admin stop admin
```

The address remains `http://127.0.0.1:8001/admin/`; `ADMIN_PORT` can change the
host port. Stop any local uvicorn process already using this port first. A
missing or incorrect reader password prevents the panel from accessing data.

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
