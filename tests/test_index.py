import pymupdf

from app.ingest.light import parse_document as parse_light
from app.rag.index import Chunk, SearchIndex, load_chunks, tokenize
from scripts.ingest import is_current
from vision_parser import write_outputs


def test_tokenize_keeps_russian_words_and_numbers() -> None:
    assert tokenize("Статус мебели 2334-26!") == ["статус", "мебели", "2334", "26"]


def test_search_returns_relevant_chunk() -> None:
    chunks = [
        Chunk(
            chunk_id="furniture-1",
            document="Реестр мебели",
            page_from=6,
            page_to=6,
            text="Статус мебели: занят, свободен, списан.",
        ),
        Chunk(
            chunk_id="trip-1",
            document="БП Командировка",
            page_from=3,
            page_to=3,
            text="Порядок оформления командировки и отчётность.",
        ),
    ]
    index = SearchIndex(chunks)

    results = index.search("какие статусы мебели", top_k=2)

    assert results
    assert results[0][1].chunk_id == "furniture-1"


def test_search_without_terms_returns_nothing() -> None:
    index = SearchIndex([])
    assert index.search("") == []


def test_prepared_chunks_are_loaded_instead_of_reparsing_pdf(tmp_path) -> None:
    pdf = tmp_path / "Документ.pdf"
    pdf.write_bytes(b"not a valid PDF")
    write_outputs(
        [
            {
                "id": "Документ.pdf:p3:c0",
                "page": 3,
                "type": "diagram",
                "title": "Схема",
                "content": "Согласование заявки",
                "data": {"nodes": ["Директор"]},
                "text": "Схема согласования заявки Директор",
            }
        ],
        pdf,
        tmp_path,
    )

    chunks = load_chunks(tmp_path)

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "Документ.pdf:p3:c0"
    assert chunks[0].document == "Документ"
    assert chunks[0].page_from == 3
    assert chunks[0].kind == "diagram"
    assert "Директор" in chunks[0].context
    assert is_current(pdf)


def test_light_ingestion_produces_searchable_chunks(tmp_path) -> None:
    pdf = tmp_path / "sample.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "The employee handbook describes how to approve a request.")
        document.save(pdf)

    prepared = parse_light(pdf, tmp_path)
    loaded = load_chunks(tmp_path)

    assert prepared
    assert loaded[0].chunk_id == prepared[0]["id"]
    assert SearchIndex(loaded).search("approve request")


def test_ingest_output_becomes_stale_after_pdf_change(tmp_path) -> None:
    pdf = tmp_path / "Документ.pdf"
    pdf.write_bytes(b"original")
    write_outputs(
        [
            {
                "id": "Документ.pdf:p1:c0",
                "page": 1,
                "type": "text",
                "title": "",
                "content": "Исходный текст",
                "data": None,
                "text": "Исходный текст",
            }
        ],
        pdf,
        tmp_path,
    )
    assert is_current(pdf)

    pdf.write_bytes(b"changed")

    assert not is_current(pdf)


def test_partial_ingest_is_not_current(tmp_path) -> None:
    pdf = tmp_path / "Документ.pdf"
    pdf.write_bytes(b"original")
    write_outputs(
        [
            {
                "id": "Документ.pdf:p1:c0",
                "page": 1,
                "type": "text",
                "title": "",
                "content": "Исходный текст",
                "data": None,
                "text": "Исходный текст",
            }
        ],
        pdf,
        tmp_path,
        complete=False,
    )

    assert not is_current(pdf)
