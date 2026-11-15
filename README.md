# Innopolis Helper Bot

Telegram assistant for internal documentation, with source citations, feedback,
support tickets, managed access, and support analytics. Docling and a remote
multimodal model prepare documents; BM25 retrieval supplies evidence to an
OpenAI-compatible answer model.

## Docker setup

### What runs

| Service | Purpose | Address |
| --- | --- | --- |
| `bot` | Telegram polling process | — |
| `postgres` | PostgreSQL database | `127.0.0.1:5438` |
| `viewer` | Signed document viewer | `http://127.0.0.1:8090` |
| `grafana` | Analytics dashboards | `http://127.0.0.1:3000/d/bot-analytics` |
| `ingest` | One-shot PDF processing; not a long-running service | — |
| `admin` | Admin panel: read-only views, allowlist/invites, document uploads | `http://127.0.0.1:8001/admin/` |
| `document-worker` | Background processing for admin document uploads | — |
| `ticket-worker` | Durable ticket delivery worker; starts by default and idles without `TICKET_PROVIDER` | — |
| `analytics` | Optional support analytics panel (`analytics` profile) | `http://127.0.0.1:8002/` |
| `analytics-labeler` | Optional background question labeler (`analytics` profile) | — |
| `llama` | Optional local llama.cpp server | `http://127.0.0.1:8080` |

Published ports bind to `127.0.0.1` only, so the stack is not exposed to the
network by default. Services with a profile are **not** started by
`docker compose up -d`; enable their profile explicitly.

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
DOCUMENTS_BASE_URL=http://127.0.0.1:8090
DOCUMENT_LINK_SECRET=replace-with-a-long-random-secret
```

`DOCUMENTS_BASE_URL` and `DOCUMENT_LINK_SECRET` enable signed source links in
bot answers. Both must be set together. For Telegram users on other devices,
use a publicly reachable HTTPS viewer URL, normally through a reverse proxy.

`TELEGRAM_ALLOWED_USERNAMES` is required for access. An empty list denies
everyone. Use Telegram usernames, not display names.

For ticket creation, also set `TICKET_PROVIDER=demo`, `jsm`, `zendesk` or
`usedesk` and the provider credentials described under
[Tickets and analytics](#tickets-and-analytics). The ticket worker starts with
the default stack; without a provider it stays idle instead of failing.

**3. Add the source PDFs.**

```bash
mkdir -p data/knowledge data/processed
```

Copy the PDFs into `data/knowledge/`. No per-file environment variables are
needed. Document IDs are derived from filenames, while existing stored manifests
keep their current IDs.

**4. Build the images.**

```bash
docker compose --profile ingest build
```

The ingestion image installs CPU-only PyTorch and LibreOffice, so the first
build takes several minutes and downloads a large amount of data.

**5. Start PostgreSQL and apply migrations.**

```bash
docker compose up -d --wait postgres
docker compose run --rm --no-deps bot alembic upgrade head
```

**6. Process PDFs from `data/knowledge` (optional if using admin uploads).**

```bash
docker compose --profile ingest run --rm ingest
```

This runs `ingest -> render -> extract` for all PDFs in `data/knowledge/`.
Extraction calls the configured multimodal model and can take a long time for
large documents. Unchanged files are cached on later runs.

You can skip this step and upload documents after startup from the admin panel:
**Document upload queue → Upload document**. Both paths produce the same
versioned chunks.

**7. Initialize Grafana access.**

```bash
docker compose run --rm --no-deps bot python scripts/setup_grafana_reader.py
```

This creates the aggregate views and the read-only `grafana_reader` role.

**8. Prepare the admin panel.**

The admin container starts with the rest of the stack and needs
`storage/admin.env`. Follow “Run the administration panel in Docker” below to
create the read-only database role and that file before starting the stack. If
the file is missing, the admin container exits but the remaining services still
start.

**9. Start the long-running services.**

```bash
docker compose up -d
```

This starts PostgreSQL, bot, viewer, Grafana, admin, `document-worker` and
`ticket-worker`.

**10. Optional: enable analytics.**

Set `ANALYTICS_PASSWORD` in `.env` to enable the analytics panel, then start
the optional analytics services:

```bash
docker compose --profile analytics up -d analytics analytics-labeler
```

The ticket worker already starts with the stack. Use `demo` for an offline
ticket trial. Real provider settings are described in
[Tickets and analytics](#tickets-and-analytics) and `docs/analytics.md`.

**11. Verify the launch.**

```bash
docker compose --profile "*" ps
docker compose logs --tail=50 bot
```

`postgres` should be `healthy`, and the bot log should contain a line like:

```text
Run polling for bot @your_bot_name
```

Open:

- Grafana: `http://localhost:3000/d/bot-analytics`
- Viewer: `http://localhost:8090`
- Admin: `http://127.0.0.1:8001/admin/`
- Analytics, if enabled: `http://127.0.0.1:8002/`

Log in to Grafana as `admin` with `GRAFANA_ADMIN_PASSWORD` from `.env`. The
analytics panel uses HTTP Basic authentication with `ANALYTICS_USERNAME`
(default `admin`) and `ANALYTICS_PASSWORD`.

The bot starts even when no chunks exist, but it cannot answer from the
knowledge base until at least one document is processed. Repeat step 6 or upload
a document through the admin panel.

On Linux, set `HOST_UID` and `HOST_GID` in `.env` to your user ID so the
ingestion container writes files with the expected ownership.

If the model endpoint runs on the host, set:

```env
LLM_BASE_URL=http://host.docker.internal:8081/v1
```

### Add or replace documents

There are two supported paths. Neither requires restarting the bot: before
each question the bot compares chunk-manifest timestamps and reloads changed
documents automatically.

#### Upload through the admin panel

1. Open `http://127.0.0.1:8001/admin/`.
2. Go to **Document upload queue**.
3. Select **Upload document**, enter a title and choose a PDF, DOCX, XLSX or
   PPTX file.
4. The upload is written to `data/inbox/`, queued in PostgreSQL and picked up
   by `document-worker`.
5. The worker runs `ingest -> render -> extract` and publishes versioned chunks
   under `data/processed/`.
6. The next bot question includes the new document. Signed source links point
   at the stored original.

Monitor the queue and worker:

```bash
docker compose logs -f document-worker
docker compose logs -f admin
```

The worker processes **one document at a time**. Large files can take a long
time, and later uploads remain `queued` until the current job finishes. This is
normal.

If a file matches an existing checksum, it is not processed. The queue records
a completed entry whose stage is `file already exists`, and the form reports:

```text
Same file already exists.
```

Each queue row shows status, progress percentage and the current stage
(`ingest`, `render`, `image analysis`, `chunking` or `completed`).

#### Process files from `data/knowledge`

The batch scanner discovers all PDFs in `data/knowledge/`. It does not scan
other formats; upload DOCX, XLSX and PPTX through the admin panel.

```bash
cp new-document.pdf data/knowledge/
docker compose --profile ingest run --rm ingest
```

Existing unchanged files use the extraction cache; new or changed files are
processed. The bot will load them on the next question. If you want to force an
immediate reload instead of waiting for the next question:

```bash
docker compose restart bot
```

Preview derived document IDs without extraction:

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

Start or stop the default stack while keeping containers and data:

```bash
docker compose stop
docker compose start
```

If analytics profile services are running, include them explicitly:

```bash
docker compose --profile "*" stop
docker compose --profile "*" start
```

Remove the default containers and network but keep the PostgreSQL and Grafana
data volumes:

```bash
docker compose down
```

If optional profile services are running, use the wildcard profile. Otherwise
Docker may leave those containers attached and report that the network is still
in use:

```bash
docker compose --profile "*" down
```

Remove the containers **and delete all stored data** (database, analytics,
tickets and Grafana state). This is destructive:

```bash
docker compose --profile "*" down -v
```

Restart one service:

```bash
docker compose restart bot
docker compose restart viewer
docker compose restart admin
docker compose restart document-worker
docker compose restart ticket-worker
docker compose restart grafana
docker compose restart postgres
docker compose --profile analytics restart analytics analytics-labeler
```

Restarting PostgreSQL can briefly interrupt the bot, viewer, admin, workers and
Grafana. They usually reconnect automatically; if not, restart them as well.

`docker compose restart` does **not** reload `.env`. After changing environment
variables, recreate the affected container:

```bash
docker compose up -d --force-recreate bot
docker compose up -d --force-recreate admin
docker compose up -d --force-recreate grafana
docker compose up -d --force-recreate ticket-worker
docker compose --profile analytics up -d --force-recreate analytics analytics-labeler
```

Stop or start an individual service without recreating it:

```bash
docker compose stop bot
docker compose start bot
docker compose stop admin
docker compose start admin
docker compose stop ticket-worker
docker compose start ticket-worker
docker compose --profile analytics stop analytics analytics-labeler
```

`ingest` is a one-shot job, not a daemon. Run it with
`docker compose --profile ingest run --rm ingest` whenever documents change.
`ticket-worker` starts with the default stack; the analytics services use the
optional `analytics` profile.

Useful diagnostics:

```bash
docker compose --profile "*" ps
docker compose logs -f bot
docker compose logs -f admin
docker compose logs -f document-worker
docker compose logs -f grafana
docker compose logs -f postgres
docker compose logs -f ticket-worker
docker compose --profile analytics logs -f analytics analytics-labeler
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

### Tickets and analytics

Ticket creation runs with the default stack; support analytics is an optional
profile. If `TICKET_PROVIDER` is empty, `ticket-worker` stays idle.

Apply migrations first when upgrading an existing installation:

```bash
docker compose run --rm --no-deps bot alembic upgrade head
```

For an offline ticket trial:

```env
TICKET_PROVIDER=demo
```

```bash
docker compose up -d ticket-worker
docker compose logs -f ticket-worker
```

Users can request support with `/operator`, by writing “оператор”, or from the
feedback actions after an unresolved answer. The request is stored in the
durable queue immediately; the worker creates it in the ticket system and stores
the external ID and status. Repeated requests reuse the existing ticket.

For a real provider, set:

```env
TICKET_PROVIDER=jsm          # or zendesk, usedesk
TICKET_BASE_URL=https://...
TICKET_API_TOKEN=...
TICKET_EMAIL=...
JIRA_SERVICE_DESK_ID=...
JIRA_REQUEST_TYPE_ID=...
USEDESK_CHANNEL_ID=...
TICKET_WEB_BASE_URL=https://...
```

Ticket tokens are secrets. Keep them in `.env`, never in commits.

Enable the analytics panel and background labeler:

```env
ANALYTICS_PASSWORD=replace-with-a-strong-password
ANALYTICS_USERNAME=admin
```

```bash
docker compose --profile analytics up -d analytics analytics-labeler
docker compose --profile analytics logs -f analytics analytics-labeler
```

The panel is available at `http://127.0.0.1:8002/` and uses HTTP Basic
authentication. Run one labeler per database; it labels new questions in the
background without delaying Telegram replies. See
[analytics and ticket setup](docs/analytics.md) for provider details, metrics,
review workflow and privacy notes.

### Rate limits

The bot allows one question every three seconds. Eight messages from the same
user within five seconds trigger a 30-second cooldown. The rate-limit reply
states the allowed interval and the remaining wait; the cooldown reply states
the message limit, the time window and the cooldown. After the first cooldown
warning, further messages during the cooldown are logged but receive no reply.
Limits are held in process memory, so they reset on restart and are not shared
between bot instances.

### Ingestion notes

The admin upload accepts PDF, DOCX, XLSX and PPTX. The batch scanner for
`data/knowledge/` discovers PDFs only; use the local pipeline below for other
formats.

## Telegram access

`TELEGRAM_ALLOWED_USERNAMES` in `.env` bootstraps the allowlist once on the
first bot startup after the allowlist migration. Entries are comma-separated;
`@` is optional and matching ignores case. After bootstrap, manage access in the admin panel's
**Allowed Telegram users** tab; changes apply without restarting the bot. Use
Telegram usernames, not display names. Users without a username cannot access
the bot unless an invite link binds their Telegram ID directly.

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
| `TELEGRAM_BOT_USERNAME` | — | Bot username without `@`, needed to create invite links |
| `ACCESS_INVITE_NOTIFY_CHAT_ID` | — | Optional Telegram chat/user ID to notify when an invite is activated |
| `TELEGRAM_ALLOWED_USERNAMES` | Empty: deny everyone | Initial comma-separated allowlist, imported once into the database |
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
| `ANALYTICS_USERNAME` | `admin` | HTTP Basic username for the analytics panel |
| `ANALYTICS_PASSWORD` | — | Password for the internal analytics panel |
| `ANALYTICS_AREAS_FILE` | — | Optional JSON product-area mapping |
| `ANALYTICS_SHOW_EXAMPLES` | `0` | Show anonymized examples in analytics |
| `ANALYTICS_DEMO` | `0` | Use generated demo cards without a database |
| `ANALYTICS_PORT` | `8002` | Analytics panel host port |
| `TICKET_PROVIDER` | Empty: disabled | `demo`, `jsm`, `zendesk`, or `usedesk` |
| `TICKET_BASE_URL` | — | Real ticket provider API base URL |
| `TICKET_API_TOKEN` | — | Real provider secret; never commit it |
| `TICKET_EMAIL` | — | Optional agent email for Jira/Zendesk authentication |
| `JIRA_SERVICE_DESK_ID` | — | Jira Service Management service desk ID |
| `JIRA_REQUEST_TYPE_ID` | — | Jira Service Management request type ID |
| `USEDESK_CHANNEL_ID` | — | Usedesk channel ID |
| `TICKET_WEB_BASE_URL` | — | Optional browser URL prefix for ticket links |

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
2. SQLite/PostgreSQL migrations are applied.
3. The LLM client is created.

The bot can start without any documents. It logs a warning and answers that no
supporting fragments were found until a document is processed. Before each
question it refreshes the versioned index, so documents added through the admin
panel or batch ingestion become available without a restart.

If a PDF in `KNOWLEDGE_DIR` has no versioned chunks, it is skipped with a
warning and remains unavailable to the bot.

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

The viewer serves signed source links for PDF and Office documents. It reads
legacy files from `KNOWLEDGE_DIR` and admin-uploaded files from the versioned
store under `DOCUMENTS_DIR`. Remote users need a reachable HTTPS URL, usually
through a reverse proxy.

## Support analytics and tickets

The optional Docker profiles are documented under
[Tickets and analytics](#tickets-and-analytics). For local development, apply
migrations and run the labeler, panel and ticket worker directly:

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m app.analytics --watch 30
.venv/bin/python -m uvicorn app.analytics.web:app --host 127.0.0.1 --port 8002
.venv/bin/python -m app.tickets.worker --watch 30
```

The analytics dashboard groups related questions and shows recurring problems,
answer outcomes, and changes between periods. A separate worker labels new
questions without delaying Telegram replies. A reviewer can confirm one
improvement task from a recurring problem; personal support requests use a
durable background queue.

Set `ANALYTICS_PASSWORD` and `TICKET_PROVIDER=demo` in `.env` for an offline
trial. The analytics panel listens on `http://127.0.0.1:8002/` and uses HTTP
Basic authentication. To see both flows without Telegram or an external API, run
`python scripts/demo_ticket_flow.py`. Jira Service Management, Zendesk and
Usedesk adapters have mock contract tests; they still need credentials and
validation against the team's test project. See
[analytics and ticket setup](docs/analytics.md).

## Local administration preview

The optional FastAPI-Amis-Admin application displays questions, documents and
observed Telegram users, and manages the bot allowlist. The `users` table is
activity data; **Allowed Telegram users** is the access list. That tab supports
manual add/edit/delete and importing `.xlsx` or `.csv` files. Its toolbar opens
the invite-link manager. Set `TELEGRAM_BOT_USERNAME` in `.env` to enable link
creation. When a user opens a valid Telegram deep link, the bot records the
activation and adds the Telegram account to the allowlist. **Invite activations**
shows those records. Optionally set `ACCESS_INVITE_NOTIFY_CHAT_ID` to receive a
Telegram notification for each activation. Links have an expiry and use limit
and can be revoked.

The ordinary views use a read-only role. Allowlist and invitation management
uses a separate role limited to the access, invitation and upload-queue tables.
The **Document upload queue** tab shows processing status; use `/documents/upload`
to submit PDF, DOCX, XLSX or PPTX files. A separate worker runs the existing
versioned ingest, render and extract pipeline. Successful imports appear in
**Documents**, become searchable without restarting the bot, and their source
links resolve to the stored original. Failed imports retain an error in the queue.

Install it in a separate environment: this admin version requires a different
FastAPI version from the one the bot image may install.

```bash
python3 -m venv storage/admin-venv
storage/admin-venv/bin/pip install -r requirements/admin.txt
```

Apply all migrations, then create the read-only role for panel
views and a scoped role for allowlist, invitation and document-upload management:

```bash
DATABASE_URL='postgresql+asyncpg://bot:OWNER_PASSWORD@127.0.0.1:5438/bot' \
  .venv/bin/alembic upgrade head

DATABASE_URL='postgresql+asyncpg://bot:OWNER_PASSWORD@127.0.0.1:5438/bot' \
ADMIN_READER_PASSWORD='READER_PASSWORD' \
  storage/admin-venv/bin/python scripts/setup_admin_reader.py

DATABASE_URL='postgresql+asyncpg://bot:OWNER_PASSWORD@127.0.0.1:5438/bot' \
ADMIN_ACCESS_DATABASE_URL='postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot' \
  storage/admin-venv/bin/python scripts/setup_admin_access.py

export ADMIN_DATABASE_URL='postgresql+asyncpg://bot_admin_reader:READER_PASSWORD@127.0.0.1:5438/bot'
export ADMIN_ACCESS_DATABASE_URL='postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot'
export ADMIN_USERNAME='admin'
export ADMIN_PASSWORD='CHOOSE_A_STRONG_PASSWORD'
storage/admin-venv/bin/uvicorn app.admin.web:create_app --factory --host 127.0.0.1 --port 8001
```

Replace the example passwords; percent-encode special characters in URLs. The
password for the panel is separate from the database password. Open
`http://127.0.0.1:8001/admin/`; the panel redirects to its English sign-in page.
For local upload processing, run `DATABASE_URL='postgresql+asyncpg://bot:OWNER_PASSWORD@127.0.0.1:5438/bot' .venv/bin/python scripts/document_worker.py` in a second terminal with the ingestion dependencies installed.
Successful sign-in creates an eight-hour, HTTP-only session cookie. Keep the
panel bound to localhost, or configure HTTPS and suitable access controls before
exposing it beyond the machine. AMIS loads interface assets from `unpkg.com` by
default.

### Run the administration panel in Docker

Prepare both database roles as shown above. Put the following values in the
ignored file `storage/admin.env` (create the directory if needed):

```env
ADMIN_DATABASE_URL=postgresql+asyncpg://bot_admin_reader:READER_PASSWORD@127.0.0.1:5438/bot
ADMIN_ACCESS_DATABASE_URL=postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot
ADMIN_USERNAME=admin
ADMIN_PASSWORD=CHOOSE_A_STRONG_PASSWORD
```

Replace the example passwords and restrict the file to its owner, for example
with `chmod 600 storage/admin.env`. Docker Compose points this URL at the
`postgres` service inside its network; the same file also works for the local
preview.

Create both roles through the bot image, using the same passwords as in
`storage/admin.env`:

```bash
docker compose run --rm --no-deps \
  -e ADMIN_READER_PASSWORD='READER_PASSWORD' \
  bot python scripts/setup_admin_reader.py

docker compose run --rm --no-deps \
  -e ADMIN_ACCESS_DATABASE_URL='postgresql+asyncpg://bot_admin_access:ACCESS_PASSWORD@127.0.0.1:5438/bot' \
  bot python scripts/setup_admin_access.py
```

The admin service starts with the normal stack. Build and control it with:

```bash
docker compose up -d --build admin
docker compose logs -f admin
docker compose stop admin
docker compose start admin
```

If `storage/admin.env` is missing, only the admin container fails; the other
services continue to run.

The address remains `http://127.0.0.1:8001/admin/`; `ADMIN_PORT` can change the
host port. Stop any local uvicorn process already using this port first. A
missing or incorrect reader password prevents the panel from accessing data.

## Documentation-only answers

The bot refuses common instruction overrides and unrelated writing/coding
requests before calling the model. Questions and source text are treated as
untrusted data. Model output must follow a fixed response schema and cite valid
source IDs; malformed or uncited output is replaced with a fixed response.
These controls reduce prompt-injection risk but do not guarantee semantic
grounding or resistance to every jailbreak. See
[the security notes](docs/prompt-injection.md) for controls and limitations.

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

### Bot has no document chunks

The bot starts even when no chunks exist, but it cannot answer from the knowledge
base. Process documents through the admin panel or run the versioned pipeline:

```bash
.venv/bin/python -m app.rag.ingest "<source>" --document-id <id>
.venv/bin/python -m app.rag.render --all
.venv/bin/python -m app.rag.extract --all
```

For Docker, an admin upload or
`docker compose --profile ingest run --rm ingest` is enough; the bot refreshes
the index before the next question.

### Admin upload stays queued

`document-worker` processes one job at a time. A large PDF blocks later uploads
until it finishes. Check:

```bash
docker compose logs -f document-worker
```

Do not delete the staged file while the job is active.

### Network is still in use

Optional analytics containers keep the Compose network attached. Stop and
remove all profiles together:

```bash
docker compose --profile "*" down
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

Check that the sender has a Telegram username and an active entry in the admin
panel's **Allowed Telegram users** tab. `TELEGRAM_ALLOWED_USERNAMES` is only used
for the one-time database bootstrap.

Keep `.env`, internal documents, and generated artifacts out of commits.
