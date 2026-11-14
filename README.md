# Innopolis Helper Bot

Telegram assistant for answering employee questions from internal documents.
The repository has two separate phases: offline PDF ingestion and a lightweight
bot runtime.

## How it works

1. PDFs in `data/knowledge/` are parsed offline by Docling. The vision model is
   used for pictures and pages Docling cannot read reliably.
2. The ingestion command writes one `<document>.chunks.jsonl` file per PDF to
   `data/processed/`. Each record keeps its source, page, readable content,
   structured data and searchable text.
3. The bot loads those JSONL files at startup, builds a BM25 search index, and
   answers questions with the configured OpenAI-compatible LLM endpoint.
4. Questions, answers and feedback are stored in SQLite. Responses can include
   signed links to the original source document when a viewer URL is configured.

The bot does not parse PDFs on startup. This keeps restarts quick and avoids
requiring the vision runtime on the bot host.

## Setup

Install the bot dependencies:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Set `TELEGRAM_BOT_TOKEN` and configure `LLM_BASE_URL`, `LLM_MODEL`, and
`LLM_API_KEY` in `.env`. By default, source PDFs are read from
`data/knowledge/`, prepared chunks from `data/processed/`, and SQLite data is
stored in `data/bot.sqlite3`.

## Prepare documents

Install the optional ingestion dependencies on the machine used to parse PDFs.
`mlx-vlm` requires Apple Silicon and macOS.

```bash
.venv/bin/python -m pip install -r requirements-ingestion.txt
.venv/bin/python -m app.ingestion
```

This parses every PDF in `KNOWLEDGE_DIR` into `CHUNKS_DIR`. To parse selected
pages from one document:

```bash
.venv/bin/python -m app.ingestion "/path/to/document.pdf" --pages 4,7-9
```

Override the input and output directories with `--knowledge-dir` and `--out-dir`
or the `KNOWLEDGE_DIR` and `CHUNKS_DIR` environment variables.

## Run the bot

After chunks have been generated:

```bash
.venv/bin/python -m app
```

The bot exits with a clear message if no prepared chunks are present. It never
falls back to a second parser or silently indexes the PDFs differently.

Optional document links require both `DOCUMENTS_BASE_URL` and
`DOCUMENT_LINK_SECRET`. The viewer can be run separately with:

```bash
.venv/bin/uvicorn app.anchors.viewer:app --host 127.0.0.1 --port 8000
```

## Project layout

```text
app/
  anchors/       Signed links and the source-document viewer
  bot/           Telegram handlers and keyboards
  ingestion/     Docling/Vision parsers and prompt templates
  llm/           OpenAI-compatible answer client
  rag/           JSONL loader and BM25 search
  storage/       SQLite/PostgreSQL question and feedback persistence
data/
  knowledge/     Source PDFs
  processed/     Generated JSONL and Markdown previews
migrations/      Alembic schema migrations
tests/           Bot, retrieval, storage and viewer tests
```

`requirements.txt` contains bot/runtime dependencies. `requirements-ingestion.txt`
adds the PDF and vision stack, and `requirements-dev.txt` adds test dependencies.
Run tests with:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
