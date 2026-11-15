# Innopolis Helper Bot

Telegram assistant for internal documentation. It answers only from loaded
documents, cites sources, collects feedback, opens support tickets, manages
Telegram access, and can show support analytics.

Documents are parsed with Docling and a remote multimodal model. Answers use
BM25 retrieval over versioned chunks and an OpenAI-compatible chat model.

## Quick start with Docker

Requirements: Docker with Compose. For a GPU-free local model server, use the
optional `llm` profile described below.

**1. Create the environment file.**

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

`DOCUMENTS_BASE_URL` and `DOCUMENT_LINK_SECRET` enable signed source links and
must be set together. On Linux, set `HOST_UID` and `HOST_GID` to your user ID so
uploaded and generated files have the expected ownership.

**2. Build the images.**

```bash
./scripts/docker_build.sh
```

This builds the default, admin and ingestion images, then removes old dangling
Docker data. The ingestion image installs CPU-only PyTorch, Docling and
LibreOffice, so the first build is large. Dependencies are cached in a separate
layer: changing `app/` no longer reinstalls them.

Equivalent manual build:

```bash
docker compose --profile ingest build
```

Docling and Hugging Face models are stored in the `ingest_cache` volume and are
downloaded only on the first extraction.

**3. Start the stack.**

```bash
docker compose up -d
docker compose ps
```

The bot applies database migrations on startup. PostgreSQL must be healthy
before the bot, workers and admin can use it. The admin container exits until
`storage/admin.env` is prepared; the other services still run.

**4. Open the services.**

| Service | Address |
| --- | --- |
| Admin panel | `http://127.0.0.1:8001/admin/` |
| Document viewer | `http://127.0.0.1:8090` |
| Grafana | `http://127.0.0.1:3000/d/bot-analytics` |

All published ports bind to `127.0.0.1`. Use a reverse proxy for remote access.

## Docker services

| Service | Purpose | Profile |
| --- | --- | --- |
| `bot` | Telegram polling and answers | default |
| `postgres` | PostgreSQL | default |
| `viewer` | Signed PDF/Office source viewer | default |
| `admin` | Admin panel | default |
| `document-worker` | Processes admin uploads | default |
| `ticket-worker` | Delivers support tickets; idles without a provider | default |
| `grafana` | Dashboards | default |
| `ingest` | One-shot bulk PDF processing | `ingest` |
| `analytics-labeler` | Classifies questions for Grafana analytics | `analytics` |
| `llama` | Optional local llama.cpp server | `llm` |

Services with a profile are not started by plain `docker compose up -d`. Enable
them explicitly, for example:

```bash
docker compose --profile analytics up -d analytics-labeler
docker compose --profile ingest run --rm ingest
```

### Optional local llama.cpp

Place the GGUF model and multimodal projector in `.models/`, set
`LLM_BASE_URL=http://llama:8080/v1`, then start the `llm` profile:

```bash
docker compose --profile llm up -d llama
```

Set `LLAMA_MODEL_FILE` and `LLAMA_MMPROJ_FILE` if the filenames differ from the
defaults. For parallel extraction, raise `LLAMA_PARALLEL` and `LLAMA_CTX`
together; each slot receives `LLAMA_CTX / LLAMA_PARALLEL`. Set
`EXTRACTION_VISION_CONCURRENCY=1` when using a single-slot server.

### Admin panel setup

The admin panel needs two scoped PostgreSQL roles and `storage/admin.env`.
The ordinary views use the read-only `bot_admin_reader` role. Allowlist,
invite, upload and document-deletion views use `bot_admin_access`.

Create `storage/admin.env` (ignored by git, `chmod 600`):

```env
ADMIN_DATABASE_URL=postgresql+asyncpg://bot_admin_reader:READER_PASSWORD@127.0.0.1:5438/bot
ADMIN_ACCESS_DATABASE_URL=postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot
ADMIN_USERNAME=admin
ADMIN_PASSWORD=CHOOSE_A_STRONG_PASSWORD
```

Create the roles through the bot image after the first stack start, when the bot
has applied migrations:

```bash
docker compose run --rm --no-deps \
  -e ADMIN_READER_PASSWORD='READER_PASSWORD' \
  bot python scripts/setup_admin_reader.py

docker compose run --rm --no-deps \
  -e ADMIN_ACCESS_DATABASE_URL='postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot' \
  bot python scripts/setup_admin_access.py
```

Replace the example passwords and percent-encode special characters in the
connection URLs.

Then start the panel:

```bash
docker compose up -d admin
docker compose logs -f admin
```

Rerun `setup_admin_access.py` after upgrading an existing installation; the
document-deletion feature needs its added table grants.

If `storage/admin.env` is missing, only the admin container fails. The rest of
the stack continues to run.

### Documents

**Admin upload (recommended).** Open **Document upload queue → Upload document**
and choose a PDF, DOCX, XLSX or PPTX. The file is staged in `data/inbox/`, queued
in PostgreSQL, and processed by `document-worker` into versioned chunks under
`data/processed/`. The next bot question uses the new document without a
restart.

The worker handles one document at a time. The queue shows 10 rows per page;
use pagination to see older uploads. Reload the queue page to refresh progress.
Stages are `ingest`, `render`, `image analysis`, `chunking` and `completed`.

If the same checksum is already stored or actively queued, the upload is
recorded as `file already exists` and is not processed.

**Delete a document.** The **Documents** tab has a **Delete** action. It removes
the document, its chunks and stored version files from the knowledge base, while
keeping upload-queue history. The same file can be uploaded again afterwards.
The admin container mounts `data/processed` read-write for this.

**Bulk ingest from `data/knowledge` (optional).** The bulk scanner processes PDFs
only:

```bash
cp new-document.pdf data/knowledge/
docker compose --profile ingest run --rm ingest
```

Existing unchanged files use the extraction cache. Preview derived document IDs
without processing:

```bash
docker compose --profile ingest run --rm ingest --dry-run
```

### Operations

```bash
docker compose ps
docker compose logs -f bot
docker compose logs -f document-worker
docker compose stop
docker compose start
```

Stop containers but keep PostgreSQL, Grafana and model-cache volumes:

```bash
docker compose --profile "*" down
```

Delete containers **and all stored data**, including the database, dashboards
and model cache. This is destructive:

```bash
docker compose --profile "*" down -v
```

After changing `.env`, recreate the affected service; `restart` does not reload
environment variables:

```bash
docker compose up -d --force-recreate bot
docker compose up -d --force-recreate admin
docker compose up -d --force-recreate document-worker
```

Rebuild after changing application code or Dockerfiles:

```bash
docker compose --profile ingest build
docker compose up -d
```

Apply migrations manually if needed. The bot also does this automatically at
startup:

```bash
docker compose run --rm --no-deps bot alembic upgrade head
```

### Docker disk maintenance

```bash
./scripts/docker_cleanup.sh
```

The script removes stopped one-shot containers, dangling images, unused networks
and build cache older than 7 days. Named volumes are never pruned. Tune it with
`BUILD_CACHE_TTL` or `--images`:

```bash
BUILD_CACHE_TTL=720h ./scripts/docker_cleanup.sh
./scripts/docker_cleanup.sh --images
./scripts/docker_cleanup.sh --all-cache
```

For an automated build with cleanup, use `./scripts/docker_build.sh`. Container
logs are capped at 3 × 10 MB per service.

## Run locally without Docker

The normal Python launch is short. Docker is optional; the bot accepts either
SQLite (default) or PostgreSQL.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/base.txt
cp .env.example .env
# edit TELEGRAM_BOT_TOKEN, LLM_BASE_URL, LLM_MODEL and LLM_API_KEY
.venv/bin/python -m app
```

The bot applies database migrations on startup and creates `data/bot.sqlite3`
when no `DATABASE_URL` is set. It can start without documents and will answer
that no supporting fragments were found.

For document processing, install the ingestion dependencies and LibreOffice for
Office files:

```bash
.venv/bin/python -m pip install -r requirements/ingestion.txt
```

Optional local viewer:

```bash
.venv/bin/python -m uvicorn app.anchors.viewer:app --host 127.0.0.1 --port 8090
```

Manual single-document pipeline, if you are not using the admin worker:

```bash
.venv/bin/python -m app.rag.ingest "data/knowledge/travel.pdf" --document-id travel
.venv/bin/python -m app.rag.render --all
.venv/bin/python -m app.rag.extract --all
```

`app.rag.extract --pages 4,7` creates a partial debug extract that the bot
ignores. `--all` commands process every stored version.

### Local admin preview

The admin panel has different FastAPI requirements from the bot, so use a
separate environment:

```bash
python3 -m venv storage/admin-venv
storage/admin-venv/bin/pip install -r requirements/admin.txt
```

Start the panel with the same `storage/admin.env` values used by Docker:

```bash
ADMIN_DATABASE_URL='postgresql+asyncpg://bot_admin_reader:READER_PASSWORD@127.0.0.1:5438/bot' \
ADMIN_ACCESS_DATABASE_URL='postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot' \
ADMIN_USERNAME=admin \
ADMIN_PASSWORD=CHOOSE_A_STRONG_PASSWORD \
  storage/admin-venv/bin/uvicorn app.admin.web:create_app \
  --factory --host 127.0.0.1 --port 8001
```

For local uploads, also run `document-worker` from the main environment with
`DATABASE_URL` and `DOCUMENTS_DIR` set. Keep the admin panel bound to localhost,
or put it behind HTTPS and access controls. AMIS loads UI assets from
`unpkg.com` by default.

## Configuration

Core variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_BOT_USERNAME` | — | Username without `@`; enables invite links |
| `TELEGRAM_ALLOWED_USERNAMES` | empty: deny everyone | One-time allowlist bootstrap |
| `ACCESS_INVITE_NOTIFY_CHAT_ID` | — | Optional Telegram chat for invite activations |
| `SUPPORT_URL` | empty | Support button destination |
| `LLM_BASE_URL` | local fallback | OpenAI-compatible base URL |
| `LLM_MODEL` | `qwen3.8-flash` | Provider model ID |
| `LLM_API_KEY` | `local` | Provider API key |
| `LLM_SESSION_ID` | `innopolis-helper-bot` | Optional routing/session header |
| `LLM_USER_AGENT` | `innopolis-helper-bot/1.0` | Client identification |
| `LLM_ANSWER_REASONING_EFFORT` | `minimal` | Answer reasoning effort |
| `LLM_EXTRACTION_REASONING_EFFORT` | `low` | Extraction reasoning effort |
| `EXTRACTION_VISION_CONCURRENCY` | `2` | Parallel picture/page vision calls |
| `EXTRACTION_TEXT_CONCURRENCY` | `4` | Parallel chunk-grouping calls |
| `EXTRACTION_REQUEST_TIMEOUT` | `180` | Extraction request timeout, seconds |
| `EXTRACTION_MIN_IMAGE_PIXELS` | `1024` | Skip tiny decorative pictures; `0` disables |
| `EXTRACTION_CACHE_ENABLED` | `true` | Cache raw extraction responses |
| `EXTRACTION_CACHE_DIR` | `~/.cache/innopolis-helper-bot/vision` | Raw response cache |
| `LLM_ENABLE_THINKING` | `auto` | Thinking mode for the tested OpenCode/qwen pair |
| `LLM_THINKING_BUDGET` | provider default (`512`) | Thinking-token budget |
| `ANSWER_MAX_COMPLETION_TOKENS` | `1400` | Thinking + answer token limit |
| `ANSWER_MAX_TOKENS` | `600` | Answer output limit |
| `TEMPERATURE` | `0.1` | Answer temperature |
| `TOP_K` | `2` | Initial retrieval seeds |
| `MIN_SCORE` | `0` | Minimum BM25 score |
| `KNOWLEDGE_DIR` | `data/knowledge` | Bulk-ingest PDF directory |
| `DOCUMENTS_DIR` | `data/processed/documents` | Versioned document store |
| `DATABASE_PATH` | `data/bot.sqlite3` | SQLite path |
| `DATABASE_URL` | — | Optional PostgreSQL URL |
| `DOCUMENTS_BASE_URL` | — | Public viewer URL for signed links |
| `DOCUMENT_LINK_SECRET` | — | HMAC secret for signed links |
| `TICKET_PROVIDER` | empty: disabled | `demo`, `jsm`, `zendesk` or `usedesk` |
| `TICKET_BASE_URL` | — | Ticket provider API URL |
| `TICKET_API_TOKEN` | — | Ticket provider secret |
| `TICKET_EMAIL` | — | Optional agent email |
| `JIRA_SERVICE_DESK_ID` | — | Jira Service Management desk ID |
| `JIRA_REQUEST_TYPE_ID` | — | Jira Service Management request type |
| `USEDESK_CHANNEL_ID` | — | Usedesk channel ID |
| `TICKET_WEB_BASE_URL` | — | Browser URL prefix for ticket links |
| `ANALYTICS_AREAS_FILE` | — | Optional product-area mapping for the labeler |

Docker-only values live in `.env` or `storage/admin.env`:

| Variable | Purpose |
| --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL password; required by Compose |
| `POSTGRES_PORT` | Host port for PostgreSQL, default `5438` |
| `GRAFANA_ADMIN_PASSWORD` | Grafana administrator password |
| `GRAFANA_DB_PASSWORD` | Grafana read-only database password |
| `GRAFANA_PORT` | Grafana host port, default `3000` |
| `VIEWER_PORT` | Viewer host port, default `8090` |
| `ADMIN_PORT` | Admin host port, default `8001` |
| `HOST_UID`, `HOST_GID` | Host ownership for ingestion files |
| `ADMIN_DATABASE_URL` | Read-only PostgreSQL URL for admin views |
| `ADMIN_ACCESS_DATABASE_URL` | Scoped URL for admin write actions |
| `LLAMA_MODEL_FILE` | GGUF model filename for the optional `llm` profile |
| `LLAMA_MMPROJ_FILE` | Multimodal projector filename for the `llm` profile |
| `LLAMA_CTX` | Total llama.cpp context size |
| `LLAMA_PARALLEL` | llama.cpp server slots; match extraction concurrency |

`LLM_SESSION_ID` and `LLM_USER_AGENT` are optional for generic providers and
required by some routed providers. Reasoning values are provider model
parameters (`none`, `minimal`, `low`, `medium`, `high`).

## Extraction and retrieval

Every document is stored as an immutable version, rendered when needed, and
extracted into versioned chunks. The bot reloads changed chunk manifests before
each question, so uploads and deletions do not require a restart.

- Docling extracts text, tables and picture regions.
- Picture regions and low-text pages are analysed by the multimodal model.
- Independent vision calls run in parallel; raw responses are cached per request.
- Only accepted, validated visual items become retrieval sources.
- BM25 supplies evidence to the answer model; answers must cite source IDs.
- Retrieval context expansion is bounded and does not truncate chunks.
- Diagram nodes/edges and table rows/columns in accepted visual chunks are
  available to the answer prompt.

Supported source formats include PDF, DOCX, XLSX, PPTX, images, text, Markdown,
HTML, structured data and source code. The admin upload accepts PDF, DOCX, XLSX
and PPTX; use the CLI for other formats. Office extraction requires LibreOffice,
either installed locally or provided by the ingestion image. Set `SOFFICE_PATH`
if `soffice` is not on `PATH`.

## Access, tickets and analytics

`TELEGRAM_ALLOWED_USERNAMES` bootstraps the allowlist once. After that, manage
access in the admin panel's **Allowed Telegram users** tab. Invite links bind a
Telegram ID to the allowlist and can have expiry/use limits. Access changes apply
without restarting the bot.

`ticket-worker` starts with the default stack and stays idle until
`TICKET_PROVIDER` is configured. It stores a durable request immediately and
records the external ticket ID after the provider confirms creation. Demo mode:

```env
TICKET_PROVIDER=demo
```

```bash
docker compose up -d ticket-worker
docker compose logs -f ticket-worker
```

Support analytics is optional. The background labeler classifies new questions
for the Grafana dashboards; it does not delay Telegram replies. Run one labeler
per database:

```bash
docker compose --profile analytics up -d analytics-labeler
docker compose --profile analytics logs -f analytics-labeler
```

Set `ANALYTICS_AREAS_FILE` to an optional JSON product-area mapping. See
[docs/analytics.md](docs/analytics.md) for ticket providers, labeling workflow
and privacy notes.

## Tests

```bash
.venv/bin/python -m pip install -r requirements/dev.txt
.venv/bin/python -m pytest
```

Offline tests do not require model credentials. Install ingestion dependencies
before running the full suite.

## Troubleshooting

**The bot has no chunks.** It starts, but cannot answer from documents until an
upload or bulk ingest succeeds. Check `docker compose logs -f document-worker`
and `docker compose ps`.

**An admin upload stays queued.** The worker processes one document at a time.
A large PDF blocks later uploads. Do not delete the staged inbox file while the
job is active.

**Duplicate upload is rejected.** A stored version or a queued/processing upload
has the same checksum. Delete the existing document in **Documents** if you want
to replace it; completed queue history does not block a re-upload.

**`Extraction is locked`.** A previous run was interrupted. Check that no worker
is running, then remove the reported `.extract.lock`, `.ingest.lock` or
`.render.lock` file.

**The model returns no JSON or empty chunks.** Confirm that the model accepts
images, check `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`, and retry. Raw
responses are cached; changing the model or prompts invalidates them.

**Office rendering fails.** Install LibreOffice and ensure `soffice` is on
`PATH`, or set `SOFFICE_PATH` / pass `--soffice`.

**Access denied.** Check the sender's Telegram username and the active entry in
**Allowed Telegram users**. The environment allowlist is only a one-time
bootstrap.

**Network is still in use after `down`.** Stop optional profiles too:

```bash
docker compose --profile "*" down
```

Keep `.env`, `storage/admin.env`, internal documents and generated artifacts out
of commits.

## Credits

The admin panel is based on
[FastAPI-Amis-Admin](https://github.com/amisadmin/fastapi-amis-admin), with the
[AMIS](https://github.com/baidu/amis) UI framework.

Additional building blocks: [aiogram](https://github.com/aiogram/aiogram) for
Telegram, [Docling](https://github.com/docling-project/docling) for document
parsing, [PyMuPDF](https://github.com/pymupdf/PyMuPDF) for PDF rendering,
[FastAPI](https://fastapi.tiangolo.com/) and
[SQLAlchemy](https://www.sqlalchemy.org/), and
[Grafana](https://grafana.com/) for dashboards.
