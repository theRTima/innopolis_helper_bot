# Innopolis Helper Bot

Telegram assistant that answers employee questions from internal documentation.

The repository contains both:

- the Telegram bot and BM25 retrieval layer;
- the document ingestion pipeline that turns PDF, Office, image and text files
  into retrieval-ready chunks.

The pipeline uses **Docling** for text, tables and document structure, and a
**remote multimodal model** for diagrams, screenshots and unreadable pages. No
local MLX model is required.

## Requirements

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

## 1. Install

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements/base.txt
.venv/bin/python -m pip install -r requirements/ingestion.txt
```

If the environment has no `pip`, install with `uv` instead:

```bash
uv pip install --python .venv/bin/python -r requirements/base.txt
uv pip install --python .venv/bin/python -r requirements/ingestion.txt
```

## 2. Configure `.env`

```bash
cp .env.example .env
```

Minimum configuration:

```env
TELEGRAM_BOT_TOKEN=...

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
`LLM_ANSWER_REASONING_EFFORT` is used when no thinking budget is active.
See `docs/answer-latency.md` for measurements and provider constraints.

## 3. Prepare documentation

The project uses one document workflow: every source is stored as an immutable
version, rendered when needed, and extracted into versioned chunks.

The examples use `travel.pdf` and `furniture.pdf` as placeholders. Replace them
with the actual files under `data/knowledge/`.

### Pipeline

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

The console scripts are also available after `pip install -e .`:

```bash
.venv/bin/rag-ingest ...
.venv/bin/rag-render ...
.venv/bin/rag-extract ...
```

### Old commands

Commands from older versions are no longer valid:

```text
python -m rag_pipeline.ingest   # old
python -m rag_pipeline.render   # old
python -m rag_pipeline.extract  # old
```

Use the `app.rag` module paths or the console scripts instead. If you see
`ModuleNotFoundError: No module named 'rag_pipeline'`, the command is outdated.

### Add another PDF

For example, to add `furniture.pdf`:

```bash
.venv/bin/python -m app.rag.ingest \
  "data/knowledge/furniture.pdf" \
  --document-id furniture
```

The ingest command prints the version path. Extract that version:

```bash
.venv/bin/python -m app.rag.extract \
  "data/processed/documents/furniture/sha256-<hash>"
```

Rendering is optional for a PDF and required for Office files:

```bash
.venv/bin/python -m app.rag.render \
  "data/processed/documents/furniture/sha256-<hash>" \
  --dpi 150
```

### Debug a single version

```bash
.venv/bin/python -m app.rag.extract \
  "data/processed/documents/travel/sha256-<hash>" \
  --pages 4,7
```

Debug extracts have `status: partial` and are ignored by the bot.

## 4. Start the bot

```bash
.venv/bin/python -m app
```

Startup checks:

1. Telegram token is present.
2. Versioned chunks exist.
3. SQLite/PostgreSQL migrations are applied.
4. The LLM client is created.

If a PDF in `KNOWLEDGE_DIR` has no versioned chunks, it is skipped with a
warning and remains unavailable to the bot. At least one ready versioned chunk
set is required for startup.

## Docker launch

To run the bot, viewer and database in containers, use this section instead of
the manual Python setup above. The Compose stack provides:

- `postgres` — database
- `bot` — Telegram polling process
- `viewer` — signed document viewer on loopback
- `ingest` — one-shot scanner that runs `ingest -> render -> extract` for every
  PDF in `data/knowledge`
- `llama` — optional local llama.cpp server under the `llm` profile

Start by creating the environment file:

```bash
cp .env.example .env
```

Set at least:

```env
TELEGRAM_BOT_TOKEN=...
LLM_BASE_URL=https://your-provider.example.com/v1
LLM_MODEL=your-multimodal-model-id
LLM_API_KEY=your-api-key
POSTGRES_PASSWORD=change-me
```

Put all PDFs into `data/knowledge/`. No per-file environment variables are
needed. Document IDs are derived from filenames, while existing stored manifests
keep their current IDs.

Docker files:

```text
Dockerfile          # bot/viewer runtime
Dockerfile.ingest   # ingestion image with Docling and LibreOffice
docker-compose.yaml
.dockerignore
```

`Dockerfile.ingest` installs CPU-only PyTorch so the image does not download
CUDA/NVIDIA libraries that are unnecessary for document extraction.

Build and start:

```bash
docker compose build
docker compose --profile ingest build
docker compose up -d postgres bot viewer
```

Process every PDF currently in `data/knowledge`:

```bash
docker compose --profile ingest run --rm ingest
```

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

The single-file helper is still available for local/manual runs:

```bash
.venv/bin/python scripts/docker_pipeline.py \
  --source "data/knowledge/travel.pdf" \
  --document-id travel
```

The Docker `ingest` service itself always scans the whole knowledge directory.

The ingestion container writes to `./data/processed`; the bot reads that host
directory read-only. `./data/knowledge` is also mounted read-only. On Linux,
set `HOST_UID` and `HOST_GID` in `.env` to your user ID so generated files keep
the expected ownership.

If the model endpoint runs on the host, set:

```env
LLM_BASE_URL=http://host.docker.internal:8081/v1
```

The optional local llama.cpp profile can be started with:

```bash
docker compose --profile llm up -d llama
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
messages from the same user within five seconds trigger a 30-second cooldown;
after the first warning, further messages during the cooldown are logged but
receive no reply. Limits are held in process memory, so they reset on restart
and are not shared between bot instances.

Office ingestion works because `Dockerfile.ingest` installs LibreOffice by
default; set `--build-arg INSTALL_OFFICE=0` to skip it.

Logs and shutdown:

```bash
docker compose logs -f bot
docker compose down
```

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

## 5. Optional document viewer

Set both values in `.env`:

```env
DOCUMENTS_BASE_URL=http://127.0.0.1:8000
DOCUMENT_LINK_SECRET=some-long-random-secret
```

Start the viewer:

```bash
.venv/bin/uvicorn app.anchors.viewer:app --host 127.0.0.1 --port 8000
```

The viewer serves signed source links for PDF and Office documents. The current
viewer reads originals from `KNOWLEDGE_DIR`; linking directly to versioned render
artifacts is a future improvement.

## Pipeline overview

```text
data/knowledge/*.pdf
        │
        ▼
app.rag.ingest
        │
        ├── app.rag.render
        │     └── pages/frames/HTML previews
        │
        └── app.rag.extract
              └── versioned chunks
        │
        ▼
app.rag.index (BM25)
        │
        ▼
app.bot.handlers + app.llm.client
        │
        ▼
Telegram answer + citations + feedback
        │
        ▼
SQLite/PostgreSQL
```

### What extraction does

1. Docling extracts text, headings, lists, tables and picture regions.
2. Running headers/footers and picture-OCR noise are removed.
3. Pictures and unreadable pages are sent to the multimodal model with
   `app/rag/extraction/prompts/image_analysis.j2`.
4. Analysis items become normalized elements with source references.
5. `app/rag/extraction/prompts/vision_chunker.j2` groups those references.
6. The application resolves references back to text and structured data.
7. The model never copies source text into the final chunk; it only groups
   references, so malformed model output cannot silently rewrite facts.
8. Missing structured elements are recovered deterministically.

### Retrieval behavior

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

## Storage layout

### Versioned store

```text
data/processed/documents/<document-id>/sha256-<hash>/
  original.<ext>
  manifest.json
  renders/render-<hash>/
    render_manifest.json
    index.html
    document.pdf            # Office only
    pages/page_0001.png
    frames/frame_0001.png
  chunks/chunk-<hash>/
    chunks.jsonl
    chunks.md
    chunk_manifest.json
```

- `manifest.json` describes the immutable upload.
- `render_manifest.json` describes a cached visual rendition.
- `chunk_manifest.json` describes a cached extraction.
- Extraction and rendering are cached by configuration hash.
- Debug extracts with `--pages` are marked `partial` and skipped by the bot.

### Chunk schema

```json
{
  "id": "travel:sha256-...:p7:c2",
  "source": "travel.pdf",
  "document_id": "travel",
  "version_id": "sha256-...",
  "page": 7,
  "page_to": 7,
  "chunk_index": 2,
  "origin": "docling",
  "type": "text",
  "title": "5 Instructions",
  "content": "...",
  "data": null,
  "text": "title + content + flattened data",
  "locator": {"kind": "pdf-page", "value": 7}
}
```

Types: `text`, `table`, `diagram`, `screenshot`, `form`, `image`, `mixed`.

Origins: `docling`, `vision`, `mixed`, `text`.

The bot searches `text` and passes `content`/`data` context to the LLM.

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

Or:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

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

### `Unprocessed PDFs: ...`

A PDF in `data/knowledge/` has no versioned chunks. Run:

```bash
.venv/bin/python -m app.rag.ingest "data/knowledge/<file>.pdf" --document-id <id>
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

## Known limitations

- Docling can misclassify a numbered heading as a list item; page-level heading
  recovery handles common cases.
- Docling only reports raster pictures. A diagram drawn entirely with vector
  shapes may not reach the vision model.
- Remote model output is nondeterministic. Structured elements that the model
  omits are recovered by deterministic fallback, but text grouping can vary
  between runs.
- Office page numbers refer to LibreOffice-rendered pages, not logical DOCX
  paragraphs or PPTX slides.
- The viewer still serves `KNOWLEDGE_DIR` originals; versioned render links are
  not wired yet.
- BM25 retrieval is lexical. There is no vector index yet.

## Repository notes

- `.env` is ignored and must not be committed.
- `data/processed/` is ignored; runtime chunks and manifests are local artifacts.
- Internal PDFs in `data/knowledge/` should not be committed.
- Do not edit files inside a stored version directory; create a new version
  instead.
