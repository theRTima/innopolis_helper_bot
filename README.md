# Innopolis Helper Bot

Telegram bot that answers questions from the PDFs in `data/knowledge/`.

## Launch locally

### 1. Install and configure

Use Python 3.11+ and copy the environment template:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Set `TELEGRAM_BOT_TOKEN` in `.env`. The defaults expect an OpenAI-compatible
LLM server at `http://127.0.0.1:8080/v1`.

### 2. Start the LLM server

Install `llama.cpp`, then start its server with a GGUF model. Set the alias to
match `LLM_MODEL` in `.env`:

```bash
llama-server -m /path/to/model.gguf --alias gemma-4-e2b --host 127.0.0.1 --port 8080
```

Keep this running in its own terminal.

### 3. Prepare the PDFs

PDF ingestion uses Docling and an MLX vision model, so run this step on
Apple Silicon macOS:

```bash
.venv/bin/python -m pip install -r requirements-ingestion.txt
.venv/bin/python -m app.ingestion
```

This creates retrieval chunks in `data/processed/`. Run it again whenever
source PDFs change. To process one PDF or selected pages:

```bash
.venv/bin/python -m app.ingestion "/path/to/document.pdf" --pages 4,7-9
```

### 4. Start the bot

```bash
.venv/bin/python -m app
```

The bot requires prepared chunks for every PDF in `data/knowledge/`. SQLite
history and feedback are stored in `data/bot.sqlite3`.

## Optional: source-document links

Set both `DOCUMENTS_BASE_URL` and `DOCUMENT_LINK_SECRET` in `.env`, then run the
viewer in a separate terminal:

```bash
.venv/bin/uvicorn app.anchors.viewer:app --host 127.0.0.1 --port 8000
```

For external users, expose the viewer over HTTPS and set `DOCUMENTS_BASE_URL`
to its public URL.

## Tests

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
