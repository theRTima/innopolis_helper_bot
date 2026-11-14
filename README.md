# openparse_test

Prototype pipeline that turns a PDF into a single markdown file using
[openparse](https://github.com/Filimoa/open-parse) for layout segmentation and a
local MLX vision model for text recognition.

Run it with:

```bash
python openparse_test.py
```

The input path is set by `PDF_PATH` at the top of `openparse_test.py`.

## Parsing logic

1. **Segment the document.** `openparse.DocumentParser` reads the PDF and splits
   it into *nodes*: semantically meaningful blocks such as headings, paragraphs,
   lists and tables. Each node carries one or more bounding boxes (`node.bbox`)
   that describe where it lives on the page(s).
2. **Render each node.** For every bbox, the script crops that region with
   PyMuPDF, converts openparse's bottom-left coordinates to PyMuPDF's top-left
   origin, adds a small `CROP_PADDING`, and saves the crop as a PNG at `DPI`.
   A node that spans several regions (or pages) produces several PNGs, which are
   passed to the model together so its content is not split apart.
3. **Analyse the image.** The first VLM pass (`prompts/image_analysis.j2`)
   returns JSON describing the crop: whether text/images/tables/equations were
   found, the extracted text and a confidence score.
4. **Convert to markdown.** The second pass (`prompts/markdown_prompt.j2`) turns
   the same images into clean markdown. The analysis JSON is used as context:
   high-confidence text is checked/format-preserved, low-confidence text is
   re-extracted, and table/LaTeX rules are enabled when the analysis flags them.
5. **Assemble the result.** Sections are joined in reading order and written to
   `<PDF name>.md` next to the script, with an HTML comment before each section
   recording the node index and source page(s).

All temporary `page_*.png` and `node_*.png` files created during a run are
deleted in a `finally` block, so only the markdown output remains even if the
model or parsing step fails.

## Chunk logic

There is no fixed-size or token-based chunking. **The openparse node is the
chunk.**

- Boundaries come from document structure (headings, spacing, layout), not from
  a character/token budget, so chunks stay semantically coherent.
- A node may cover multiple bounding boxes or pages; all of its regions are
  rendered and sent to the model as one unit, then emitted as one section.
- Chunks are written sequentially into a single markdown file:
  `<!-- node N (page ...) -->` followed by that chunk's markdown.
- There is no overlap between chunks and no automatic merging of short nodes.
  If a downstream consumer needs smaller pieces, split the resulting markdown
  further.

## Configuration

| Constant | Purpose |
| --- | --- |
| `PDF_PATH` | Input PDF |
| `DPI` | Rasterisation resolution for page and node images |
| `MAX_TOKENS` | Max tokens per model response |
| `CROP_PADDING` | Extra pixels around a node bbox so glyph edges are not clipped |
| `model_path` | MLX-VLM model id |
