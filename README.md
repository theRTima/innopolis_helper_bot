# Innopolis Helper Bot

Telegram bot that answers questions from PDFs in `data/knowledge/`.

## Run locally

Use Python 3.11+. Create an environment, install the project and bot
dependencies, and configure the token:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Set `TELEGRAM_BOT_TOKEN` in `.env`. The bot connects to an OpenAI-compatible
LLM endpoint at `http://127.0.0.1:8080/v1`; set `LLM_MODEL` to the model ID
served by MLX-LM.

Start the MLX-LM server in a separate terminal on Apple Silicon macOS:

```bash
.venv/bin/python -m pip install mlx-lm
.venv/bin/mlx_lm.server --model /path/to/model --host 127.0.0.1 --port 8080
```

Install the PDF-ingestion dependencies and prepare chunks:

```bash
.venv/bin/python -m pip install -r requirements-ingestion.txt
.venv/bin/python -m app.ingestion
```

Run ingestion again when PDFs change. To process one PDF or selected pages:

```bash
.venv/bin/python -m app.ingestion "/path/to/document.pdf" --pages 4,7-9
```

Start the bot:

```bash
.venv/bin/python -m app
```

The bot requires prepared chunks for every PDF in `data/knowledge/`. SQLite
history and feedback are stored in `data/bot.sqlite3`.

## Versioned document pipeline

`rag_pipeline` validates and snapshots supported files, renders previews, and
creates versioned retrieval chunks. The bot prefers these chunks and falls back
to the older flat `data/processed/*.chunks.jsonl` files during migration.
Office previews and Office extraction require LibreOffice.

```bash
.venv/bin/rag-ingest "/path/to/document.pdf" --document-id travel
.venv/bin/rag-render "/path/from/ingest/output" --dpi 150
.venv/bin/rag-extract "/path/from/ingest/output"
# Or process all stored versions:
.venv/bin/rag-render --all
.venv/bin/rag-extract --all
```

Each version gets its own cacheable chunk set:

```text
data/processed/documents/<document-id>/sha256-<hash>/
  original.*
  manifest.json
  renders/render-<hash>/
  chunks/chunk-<hash>/
    chunks.jsonl
    chunks.md
    chunk_manifest.json
```

Extraction uses Docling for structure and the normalized vision prompts for
pictures and unreadable pages. `python -m app.ingestion` remains available as a
legacy flat-output command, but `rag-extract` is the versioned path.

To inspect one version without touching production chunks, use a debug page
selection:

```bash
.venv/bin/rag-extract "/path/to/version" --pages 4,7
```

Debug extracts have `status: partial` and are ignored by the bot.

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
