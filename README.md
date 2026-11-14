# innopolis_helper_bot

Telegram assistant for the organisation. This repository contains the document
ingestion layer: it turns internal PDFs into retrieval-ready chunks that the bot
can use to answer employee questions.

## Why vision-as-parser

The documents contain more than plain text: process diagrams, screenshots of
internal systems, forms and output-file icons. A text-only parser (pdfminer,
openparse, etc.) either skips those pages or returns only the heading, so the
bot cannot answer questions about the actual process logic.

`vision_parser.py` therefore uses the vision model as **both the parser and the
chunker**. The model reads the page image, decides where semantic chunks begin
and end, and writes down everything it sees. Diagrams are stored both as a
readable explanation and as machine-readable graph data (`nodes` / `edges`), so
the bot does not need a human-friendly rendering to reason about them.

## Pipeline

1. **Render the page.** Each selected page is rasterised at 200 DPI.
2. **Separate raster images from text.** Large embedded images (diagrams,
   screenshots) are extracted at their **native resolution**. The page render
   has those areas whited out, so the page pass only sees text, tables, forms
   and vector graphics. This avoids sending the same diagram twice and prevents
   the model from describing a tiny, unreadable copy of it.
3. **Vision chunking.** The page image goes to the VLM with
   `prompts/vision_chunker.j2`. The model returns a JSON array of self-contained
   chunks with `type`, `title`, `content` and optional structured `data`.
4. **Diagram enrichment.** An embedded diagram first goes through
   `prompts/image_analysis.j2` (labels, connections, business logic), then
   through the chunker with that analysis as ground truth. This two-pass step is
   what reliably recovers every box, decision and branch.
5. **Clean-up.** Running headers/footers, export stamps and the artificial Word
   heading error are removed. Heading-only chunks are merged into the chunk they
   introduce. Oversized text chunks are split at line boundaries as a safety
   net.
6. **Write outputs.** Chunks are written to `<name>.chunks.jsonl` (for the bot /
   vector index) and `<name>.chunks.md` (for debugging by humans).

All temporary page/image PNGs are deleted after the run, even on failure.

## Chunk schema

```json
{
  "id": "БП Командировка.pdf:p4:c0",
  "source": "БП Командировка.pdf",
  "page": 4,
  "chunk_index": 0,
  "origin": "image",
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

- `content` is the human-readable transcription or explanation.
- `data` keeps non-text structure machine-readable: graph nodes/edges for
  diagrams, columns/rows for tables, fields/buttons/paths for forms and
  screenshots.
- `text` is `title + content + flattened data`, ready to send to an embedding
  model. The bot should retrieve on this field and pass `content` + `data` to
  the answer prompt.

## Chunking logic

There is no fixed token window and no embedding-based semantic segmentation.
**The vision model is the chunker.**

- Chunks are cut at semantic boundaries: heading/section, paragraph, table,
  diagram, form or screenshot.
- Tables, diagrams, forms and lists are never split across chunks.
- A heading is merged into the chunk it introduces, never stored alone.
- Target size is roughly 64-1024 tokens. Dense pages should produce several
  chunks; a line-based splitter only catches overflow.
- Each embedded image becomes its own chunk, processed at native resolution.
- Every chunk repeats enough context (section title, object names) to be
  understood in isolation, because retrieval may return it alone.
- The original language is preserved (Russian stays Russian).

## Usage

```bash
# all pages
python vision_parser.py "/path/to/document.pdf"

# specific pages (1-based), useful while testing
python vision_parser.py "/path/to/document.pdf" --pages 4,7-9

# choose output directory
python vision_parser.py "/path/to/document.pdf" --out-dir output
```

Default PDF and model are set at the top of `vision_parser.py`. The model is
loaded lazily on the first vision call.

### Configuration

| Constant | Purpose |
| --- | --- |
| `DEFAULT_PDF` | PDF used when no path is passed |
| `MODEL_ID` | MLX-VLM model id |
| `DPI` | Page rasterisation resolution |
| `MAX_TOKENS` | Max tokens per model response |
| `IMAGE_AREA_RATIO` | Minimum page share for an embedded image to be treated as content |
| `MAX_CHUNK_CHARS` | Safety split for oversized text chunks |

## Legacy prototype

`openparse_test/openparse_test.py` is the earlier text-segmentation approach
(openparse nodes + two VLM passes). It is kept for reference. It cannot chunk
by vision and relies on fallbacks for image-only pages, so `vision_parser.py`
is the recommended entry point.

## Known limitations

- The local 4-bit model occasionally misreads similar-looking characters or
  Russian words in dense screenshots. Spot-check critical instructions.
- The chunker may return one large chunk for a dense page; the overflow splitter
  keeps it usable, but a stronger model or a second chunking pass would improve
  boundaries.
- Embeddings and the retrieval/answer loop are downstream of this repository's
  output; this layer only produces the chunks.
