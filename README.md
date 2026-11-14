# innopolis_helper_bot

Telegram assistant for the organisation. The bot searches PDF chunks with an
in-memory BM25 index. Chunks are prepared offline by `scripts/ingest.py`, which
uses `docling_parser.py` when Docling is installed and otherwise falls back to
the lighter PyMuPDF pipeline in `app/ingest/light.py`. Both paths reuse
`vision_parser.py` for pictures and image-only pages.

The vision code supports `mlx_vlm` on Apple Silicon and the local llama.cpp
Gemma server on Linux. The bot consumes prepared `.chunks.jsonl` files; a PDF
without one is parsed with OpenParse at startup as a legacy fallback. Docling
and MLX dependencies are intentionally separate from `requirements.txt`.

## Docling first, vision only when needed

The documents contain normal text and tables, but also process diagrams,
screenshots of internal systems, forms and output-file icons. A pure text parser
skips the non-text parts; a pure vision parser is slower and can misread dense
text.

`docling_parser.py` therefore uses a hybrid pipeline:

- **Docling** reads the document the normal way: text, lists, headings, reading
  order and table structure.
- **The vision model** is used only where Docling cannot produce usable content:
  pictures/diagrams and pages with no readable text layer.

The result is one JSONL file of self-contained chunks, with text, tables and
diagrams all represented in a machine-usable form.

## Pipeline

1. **Docling conversion.** `DocumentConverter` parses the selected page range.
   It extracts text, headings, lists, tables and picture regions.
2. **Noise filtering.** Running headers/footers (`FURNITURE`) are dropped, as are
   the OCR text items Docling creates *inside* pictures — those are usually
   garbage (`"BunoneO"`, `"Cornacoae"`) and are replaced by real vision analysis.
3. **Text chunking.** Tables are removed from the chunker input and
   `HybridChunker` splits the remaining text/list content. Each chunk gets the
   nearest section heading, a page number and a stable id.
4. **Table chunks.** Each real table becomes one chunk with a clean markdown
   rendering in `content` and machine-readable `columns` / `rows` in `data`.
   This avoids the default table serialisation, which loses the row structure.
   A paragraph or a two-column layout that Docling misdetects as a table is
   emitted as ordinary text instead of fake columns.
5. **Picture chunks.** Every `PictureItem` is matched to its embedded image in
   the source PDF and extracted at **native resolution** (falling back to a
   region render if there is no embedded bitmap). It then goes through
   `prompts/image_analysis.j2` and `prompts/vision_chunker.j2`. Diagrams are
   stored as a readable explanation plus graph data (`nodes` / `edges`).
6. **Page fallback.** A page with less than `MIN_PAGE_TEXT` readable characters,
   no table and no picture is rendered and sent to the vision chunker as a
   whole page.
7. **Merge and clean.** Text, table and vision chunks are merged in page and
   reading order. Headers/footers, export stamps and the artificial Word heading
   error are removed; heading-only chunks are merged into the chunk they
   introduce; oversized text chunks are split at line boundaries.
8. **Write outputs.** Chunks are written to `<name>.chunks.jsonl` (for the bot /
   vector index) and `<name>.chunks.md` (for debugging by humans).

All temporary PNGs are deleted after the run, even on failure.

## Chunk schema

```json
{
  "id": "БП Командировка.pdf:p4:c0",
  "source": "БП Командировка.pdf",
  "page": 4,
  "chunk_index": 0,
  "origin": "vision",
  "type": "diagram",
  "title": "2 Схема бизнес-процесса",
  "content": "Step-by-step explanation of the process ...",
  "data": {
    "nodes": [
      {"id": "start", "label": "Инициация БП", "kind": "start"},
      {"id": "approve", "label": "Согласование с Директором БП", "kind": "action"}
    ],
    "edges": [
      {"from": "start", "to": "approve", "label": ""}
    ]
  },
  "text": "Text that is embedded into the vector index"
}
```

- `origin` is `docling` for text/table chunks, `vision` for pictures and
  `vision_page` for full-page fallback.
- `content` is the human-readable transcription or explanation.
- `data` keeps non-text structure machine-readable: graph nodes/edges for
  diagrams, columns/rows for tables, fields/buttons/paths for forms and
  screenshots.
- `text` is `title + content + flattened data`, ready to send to an embedding
  model. The bot should retrieve on this field and pass `content` + `data` to
  the answer prompt.

## Chunking logic

Text uses Docling's `HybridChunker`, so chunk boundaries follow headings,
paragraphs and lists rather than a fixed character window. On top of that:

- Tables are never split by the text chunker; each is one structured chunk.
- Pictures are never split; each is one vision chunk (or several semantic
  chunks, if the model returns them).
- A section heading is repeated in the chunk so it can be understood when
  retrieved alone. Docling sometimes misclassifies a numbered heading as a list
  item, so the page's first section-like line is also propagated to continuation
  pages as fallback context.
- The original language is preserved (Russian stays Russian).
- A line-based splitter catches only oversized text chunks as a safety net.

## Usage

```bash
# Prepare every PDF in data/knowledge for the bot.
.venv/bin/python scripts/ingest.py

# Prepare one file or force a rebuild after changing it.
.venv/bin/python scripts/ingest.py "data/knowledge/БП Командировка.pdf" --force

# Use a specific parser instead of auto-detecting Docling.
.venv/bin/python scripts/ingest.py --parser light
```

`scripts/ingest.py` writes `<name>.chunks.jsonl` and
`<name>.chunks.meta.json` next to each PDF. The metadata stores the source and
chunk checksums, so the bot ignores stale or partial output and falls back to
OpenParse instead. The heavy parsing runs offline, not on every user question.

The vision step uses `VISION_BACKEND`:

- `llama_cpp` (default on Linux) sends rendered images to the local
  OpenAI-compatible llama.cpp server from `VISION_BASE_URL`.
- `mlx` uses the MLX-VLM model on Apple Silicon.

```bash
# Docling-first pipeline directly (recommended when Docling is installed)
python docling_parser.py "/path/to/document.pdf"

# specific pages (1-based), useful while testing
python docling_parser.py "/path/to/document.pdf" --pages 4,7-10
```

`vision_parser.py` can still be run on its own as a vision-only parser, and its
model loading, rendering, prompt handling and writers are reused by
`docling_parser.py` and `app/ingest/light.py`.

```bash
# vision-only mode, useful for comparing results
python vision_parser.py "/path/to/document.pdf" --pages 4
```

### Configuration

| Variable | Purpose |
| --- | --- |
| `VISION_BACKEND` | `llama_cpp` (default on Linux) or `mlx` |
| `VISION_BASE_URL` | OpenAI-compatible endpoint for the vision model |
| `VISION_MODEL` | Model name sent to that endpoint |
| `VISION_MAX_TOKENS` | Max tokens per vision response |

| Constant | File | Purpose |
| --- | --- | --- |
| `DEFAULT_PDF` | `docling_parser.py` | PDF used when no path is passed |
| `MIN_PAGE_TEXT` | `docling_parser.py` | Below this many readable characters a page goes to vision fallback |
| `PICTURE_IOU_DEDUP` | `docling_parser.py` | Overlap threshold for deduplicating picture items |
| `MAX_TABLE_CELL_CHARS` | `docling_parser.py` | A longer cell means Docling likely mistook text for a table |
| `MODEL_ID` | `vision_parser.py` | MLX-VLM model id |
| `DPI` | `vision_parser.py` | Rasterisation resolution for page and region renders |
| `MAX_TOKENS` | `vision_parser.py` | Max tokens per model response |
| `IMAGE_AREA_RATIO` | `vision_parser.py` | Minimum page share for the vision-only parser |
| `MAX_CHUNK_CHARS` | `vision_parser.py` | Safety split for oversized text chunks |

The vision model is loaded lazily on the first vision call, so text-only PDFs
never pay the model-loading cost.

## Legacy prototype

`openparse_test/openparse_test.py` is the earlier openparse approach (openparse
nodes + two VLM passes). It is kept for reference only. `docling_parser.py` is
the recommended entry point.

## Known limitations

- Docling sometimes labels a numbered section heading as a list item or misses
  it entirely. The page-context fallback recovers the common cases, but a title
  can still be generic (`Страница 10`) on continuation pages.
- Docling only reports raster pictures. A diagram drawn entirely with vector
  shapes may be treated as text and never reach the vision model.
- The local 4-bit vision model occasionally misreads similar-looking characters
  or Russian words in dense screenshots. Spot-check critical instructions.
- A page is sent to full-page vision only when Docling finds almost no text and
  no picture. A low-quality scan that Docling OCRs into plausible-looking text
  will stay on the Docling path.
- Embeddings and the retrieval/answer loop are downstream of this repository's
  output; this layer only produces the chunks.

:)
