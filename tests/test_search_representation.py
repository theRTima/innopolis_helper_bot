from app.rag.context import format_context
from app.rag.index import Chunk, SearchIndex
from app.rag.representation import is_navigation


def chunk(name, text, kind="text", title=""):
    return Chunk(
        chunk_id=name,
        document="Example",
        page_from=1,
        page_to=1,
        text=text,
        chunk_type=kind,
        title=title,
    )


def test_navigation_does_not_outrank_actual_diagram():
    contents = chunk(
        "toc", "Схема бизнес-процесса ........ 4", title="Table of Contents"
    )
    diagram = chunk(
        "diagram", "Бизнес-процесс: Start -> Decision -> End", "diagram"
    )
    assert (
        SearchIndex([contents, diagram]).search(
            "Опиши схему бизнес-процесса", top_k=1
        )[0][1]
        == diagram
    )
    assert (
        SearchIndex([contents, diagram]).search(
            "Покажи table of contents", top_k=1
        )[0][1]
        == contents
    )


def test_contents_heading_does_not_hide_actual_instructions():
    source = chunk(
        "schedule",
        "Для оперативной работы введено дежурство " "офис-менеджеров. " * 4,
        title="1 Содержание",
    )
    assert not is_navigation(source)


def test_visual_translation_is_search_only():
    image = chunk("icon", "A paperclip icon is visible.", "image")
    source_text = image.text
    result = SearchIndex([image]).search("Как выглядит значок вложения?")
    assert result[0][1] == image
    assert "скрепка" not in format_context(result)
    assert image.text == source_text
    assert (
        SearchIndex([image], use_search_representation=False).search(
            "Как выглядит значок вложения?"
        )
        == []
    )


def test_unknown_question_does_not_create_a_match():
    index = SearchIndex([chunk("schedule", "Дежурство офис-менеджеров")])
    assert index.search("квантовая запутанность фотонов") == []


def test_expansion_does_not_modify_question_or_evidence():
    source = chunk("creation", "Нажать Создать запрос")
    query = "Как создать заявку?"
    result = SearchIndex([source]).search(query)
    assert result[0][1].text == source.text
    assert query == "Как создать заявку?"


def test_evaluation_does_not_count_same_page_of_other_document(tmp_path):
    import json
    from app.rag.evaluate import evaluate
    from test_versioned_index import _chunk, _version

    root = tmp_path / "documents"
    target = _chunk("travel", source="travel.pdf")
    target.update(document_id="travel", page=4, text="бюджет")
    other = _chunk("other", source="other.pdf")
    other.update(document_id="other", page=4, text="Схема процесса")
    _version(root, "travel", "sha256-v1", "2026-01-01", [target])
    _version(root, "other", "sha256-v1", "2026-01-01", [other])
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            {
                "document_id": "travel",
                "cases": [
                    {
                        "id": "diagram",
                        "question": "схема",
                        "evidence_groups": [[4]],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report = evaluate(root, cases, top_k=1)
    assert report["cases"][0]["enhanced"]["primary_hit"] is False
    assert report["cases"][0]["enhanced"]["evidence_group_coverage"] == 0
