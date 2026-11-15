import pytest
from fastapi.testclient import TestClient
import zipfile

from app.anchors import Locator, LocatorKind, build_anchor_url, sign_document_id
from app.anchors.viewer import create_app
from app.anchors.office import render_docx, render_pptx, render_xlsx


def test_pdf_anchor_points_to_page_without_reuploading_document() -> None:
    url = build_anchor_url(
        "https://docs.example.org",
        "furniture-registry-v2",
        Locator(LocatorKind.PDF_PAGE, 6),
        secret="test-secret",
        now=1000,
    )

    assert url.startswith(
        "https://docs.example.org/documents/furniture-registry-v2?expires=605800&signature="
    )
    assert url.endswith("#page=6")


def test_office_locators_are_encoded_as_viewer_fragments() -> None:
    docx = build_anchor_url(
        "https://docs.example.org",
        "trip-policy",
        Locator(LocatorKind.DOCX_PARAGRAPH, "travel-expenses-3"),
        secret="test-secret",
        now=1000,
    )
    xlsx = build_anchor_url(
        "https://docs.example.org",
        "furniture-register",
        Locator(LocatorKind.XLSX_RANGE, "Склад 1", "A2:C5"),
        secret="test-secret",
        now=1000,
    )

    assert docx.endswith("#paragraph=travel-expenses-3")
    assert xlsx.endswith("#sheet=%D0%A1%D0%BA%D0%BB%D0%B0%D0%B4+1&range=A2%3AC5")
    assert "expires=605800" in docx
    assert "signature=" in docx


def test_invalid_locator_is_rejected() -> None:
    with pytest.raises(ValueError):
        Locator(LocatorKind.PDF_PAGE, 0)


def test_viewer_index_reports_running(tmp_path) -> None:
    client = TestClient(create_app(tmp_path, "test-secret"))
    response = client.get("/")
    assert response.status_code == 200
    assert "Document viewer is running" in response.text


def test_viewer_serves_only_signed_known_pdf(tmp_path) -> None:
    (tmp_path / "policy.pdf").write_bytes(b"%PDF-test")
    client = TestClient(create_app(tmp_path, "test-secret"))
    expires = 4_000_000_000
    signature = sign_document_id("policy", "test-secret", expires)

    page = client.get(
        f"/documents/policy?expires={expires}&signature={signature}#page=3"
    )
    file_response = client.get(
        f"/documents/policy/file?expires={expires}&signature={signature}"
    )

    assert page.status_code == 200
    assert "/documents/policy/file?" in page.text
    assert file_response.status_code == 200
    assert file_response.content == b"%PDF-test"
    assert file_response.headers["cache-control"] == "private, no-store"


def test_viewer_rejects_invalid_signature_and_path_traversal(tmp_path) -> None:
    client = TestClient(create_app(tmp_path, "test-secret"))

    assert client.get("/documents/unknown?expires=4000000000&signature=bad").status_code == 403
    signed_traversal = sign_document_id("..%2F.env", "test-secret", 4_000_000_000)
    assert client.get(
        f"/documents/..%252F.env?expires=4000000000&signature={signed_traversal}"
    ).status_code in {404, 422}


def test_docx_renderer_creates_paragraph_anchor(tmp_path) -> None:
    path = tmp_path / "guide.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Командировка</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Сохраняемый абзац</w:t></w:r></w:p></w:body></w:document>',
        )

    html = render_docx(path)

    assert 'id="paragraph-1"' in html
    assert "Сохраняемый абзац" in html


def test_xlsx_renderer_creates_sheet_and_range_anchor(tmp_path) -> None:
    path = tmp_path / "register.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Склад" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Стул</t></is></c></row>'
            '</sheetData></worksheet>',
        )

    html = render_xlsx(path)

    assert 'data-sheet-name="Склад"' in html
    assert 'data-cell="A1"' in html
    assert "Стул" in html


def test_pptx_renderer_creates_slide_anchor(tmp_path) -> None:
    path = tmp_path / "training.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide2.xml",
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
            '<p:sp><p:txBody><a:p><a:r><a:t>Кнопка добавления</a:t></a:r></a:p></p:txBody></p:sp>'
            '</p:spTree></p:cSld></p:sld>',
        )

    html = render_pptx(path)

    assert 'id="slide-2"' in html
    assert "Кнопка добавления" in html


def test_viewer_serves_office_as_html_at_signed_document_url(tmp_path) -> None:
    path = tmp_path / "guide.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>Фрагмент</w:t></w:r></w:p></w:body></w:document>',
        )
    client = TestClient(create_app(tmp_path, "test-secret"))
    expires = 4_000_000_000
    signature = sign_document_id("guide", "test-secret", expires)

    response = client.get(f"/documents/guide?expires={expires}&signature={signature}#paragraph=1")

    assert response.status_code == 200
    assert 'id="paragraph-1"' in response.text
