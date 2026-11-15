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

The same model settings are used by:

- the bot for answer generation;
- `app.ingestion` for diagrams and page fallback;
- `app.rag.extract` for the normalized vision pipeline.

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
| `ANSWER_MAX_TOKENS` | `600` | Answer generation limit |
| `TEMPERATURE` | `0.1` | Answer generation temperature |
| `TOP_K` | `6` | Number of chunks passed to the LLM |
| `MIN_SCORE` | `0` | Minimum BM25 score |
| `KNOWLEDGE_DIR` | `data/knowledge` | Source PDF directory for the legacy flat pipeline |
| `CHUNKS_DIR` | `data/processed` | Legacy flat chunk directory |
| `DOCUMENTS_DIR` | `data/processed/documents` | Versioned `app/rag` store |
| `DATABASE_PATH` | `data/bot.sqlite3` | SQLite path |
| `DATABASE_URL` | — | Optional PostgreSQL URL |
| `DOCUMENTS_BASE_URL` | — | Public viewer URL for signed source links |
| `DOCUMENT_LINK_SECRET` | — | HMAC secret for source links |

`LLM_SESSION_ID` and `LLM_USER_AGENT` are required by some providers (for
example, OpenCode Go uses `x-opencode-session` for routing). They are harmless
for generic OpenAI-compatible providers.

## 3. Prepare documentation

There are two supported workflows. The versioned `app/rag` workflow is
recommended; the flat `app.ingestion` workflow remains for migration and simple
PDF-only deployments.

### Recommended: versioned pipeline

```bash
# 1. Store an immutable version
.venv/bin/python -m app.rag.ingest \
  "data/knowledge/БП Командировка.pdf" \
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

### Legacy: flat PDF chunks

```bash
.venv/bin/python -m app.ingestion
```

Process one PDF or selected pages:

```bash
.venv/bin/python -m app.ingestion "data/knowledge/Реестр мебели.pdf"
.venv/bin/python -m app.ingestion "data/knowledge/БП Командировка.pdf" --pages 4,7-9
```

This writes:

```text
data/processed/<pdf-stem>.chunks.jsonl
data/processed/<pdf-stem>.chunks.md
```

The bot prefers versioned chunks and automatically fills in flat chunks for
documents that have not been migrated yet.

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
2. Chunks exist.
3. Every PDF in `KNOWLEDGE_DIR` has either versioned or flat chunks.
4. SQLite/PostgreSQL migrations are applied.
5. The LLM client is created.

The bot refuses to start if a document is missing from the chunk store.

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
        ├── legacy ── app.ingestion ── data/processed/*.chunks.jsonl
        │
        └── versioned ── app.rag.ingest
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
   `app/ingestion/prompts/image_analysis.j2`.
4. Analysis items become normalized elements with source references.
5. `app/ingestion/prompts/vision_chunker.j2` groups those references.
6. The application resolves references back to text and structured data.
7. The model never copies source text into the final chunk; it only groups
   references, so malformed model output cannot silently rewrite facts.
8. Missing structured elements are recovered deterministically.

### Retrieval behavior

- `TOP_K` defaults to `6`, so several text and visual chunks reach the LLM.
- Table-of-contents chunks are skipped for normal questions; they are used only
  when the query asks for contents, sections or structure.
- Diagram/image/screenshot chunks get a small BM25 boost for their title and
  document name, because long visual chunks are otherwise penalised by length.
- Visual queries such as “схема”, “диаграмма”, “рисунок”, “процесс” or “шаги”
  always receive the best matching visual chunks, even if BM25 ranks several
  text chunks above them.
- The answer prompt explicitly tells the LLM to use diagram nodes/edges and
  table rows/columns when present.

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
  "source": "БП Командировка.pdf",
  "document_id": "travel",
  "version_id": "sha256-...",
  "page": 7,
  "page_to": 7,
  "chunk_index": 2,
  "origin": "docling",
  "type": "text",
  "title": "5 Инструкция",
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

Install LibreOffice, for example:

```bash
# macOS
brew install --cask libreoffice

# Ubuntu/Debian
sudo apt install libreoffice
```

Set `SOFFICE_PATH` if `soffice` is not on `PATH`.

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

### `TELEGRAM_BOT_TOKEN не задан`

Create `.env` from `.env.example` and put the token from @BotFather into
`TELEGRAM_BOT_TOKEN`.

### `Чанки не найдены`

Run one of:

```bash
.venv/bin/python -m app.ingestion
# or the full versioned pipeline:
.venv/bin/python -m app.rag.extract --all
```

### `Не обработаны PDF: ...`

A PDF in `data/knowledge/` has no matching chunks. Run the legacy ingestion or
migrate it through `app.rag.ingest` → `render` → `extract`.

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
