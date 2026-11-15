import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from PIL import Image
from pptx import Presentation
from pypdf import PdfWriter

from app.rag.ingest import IngestionError, ingest_document


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = self.root / "storage"

    def ingest(self, path, **kwargs):
        return ingest_document(path, "example", self.storage, **kwargs)

    def test_docx_with_table_and_image(self):
        image = self.root / "picture.png"
        Image.new("RGB", (20, 30)).save(image)
        path = self.root / "manual.docx"
        doc = Document()
        doc.add_paragraph("Instruction")
        doc.add_table(rows=2, cols=2)
        doc.add_picture(str(image))
        doc.save(path)
        result = self.ingest(path)
        self.assertEqual(result["manifest"]["format"], "docx")
        self.assertIsNone(result["manifest"]["page_count"])
        self.assertEqual(result["manifest"]["validation"]["table_count"], 1)
        self.assertEqual(
            (Path(result["version_path"]) / "original.docx").read_bytes(),
            path.read_bytes(),
        )
        self.assertTrue(self.ingest(path)["duplicate"])

    def test_xlsx_and_pptx(self):
        workbook = Workbook()
        workbook.active["A1"] = "=1+1"
        workbook.create_sheet("Second")
        xlsx = self.root / "table.xlsx"
        workbook.save(xlsx)
        workbook.close()
        result = self.ingest(xlsx)
        self.assertEqual(result["manifest"]["validation"]["sheet_count"], 2)
        self.assertIsNone(result["manifest"]["page_count"])
        presentation = Presentation()
        presentation.slides.add_slide(presentation.slide_layouts[6])
        pptx = self.root / "slides.pptx"
        presentation.save(pptx)
        result = self.ingest(pptx)
        self.assertEqual(result["manifest"]["validation"]["slide_count"], 1)

    def test_text_and_code_are_preserved_without_execution(self):
        for name, content in (
            ("README.md", "# Title\n\nТекст"),
            ("script.py", "raise RuntimeError('must not execute')\n"),
            ("Dockerfile", "FROM python:3.13\n"),
        ):
            path = self.root / name
            path.write_text(content, encoding="utf-8")
            result = self.ingest(path)
            self.assertEqual(result["manifest"]["source_kind"], "text")
            saved = (
                Path(result["version_path"])
                / result["manifest"]["original_path"]
            )
            self.assertEqual(saved.read_bytes(), path.read_bytes())

    def test_text_encodings(self):
        path = self.root / "legacy.txt"
        path.write_bytes("Привет".encode("cp1251"))
        with self.assertRaises(IngestionError):
            self.ingest(path)
        self.assertEqual(
            self.ingest(path, encoding="cp1251")["manifest"]["validation"][
                "encoding"
            ],
            "cp1251",
        )
        for encoding in ("utf-8-sig", "utf-16", "utf-32"):
            path.write_bytes("Привет".encode(encoding))
            self.assertEqual(
                self.ingest(path)["manifest"]["validation"]["character_count"],
                6,
            )

    def test_structured_text_syntax(self):
        for name, valid, invalid in (
            ("api.json", '{"x": 1}', "{broken"),
            ("data.jsonl", '{"x": 1}\n{"y": 2}', '{"x": 1}\nbroken'),
            ("config.toml", "x = 1", "x = ["),
            ("data.xml", "<root/>", "<root>"),
        ):
            path = self.root / name
            path.write_text(valid, encoding="utf-8")
            self.ingest(path)
            path.write_text(invalid, encoding="utf-8")
            with self.assertRaises(IngestionError):
                self.ingest(path)

    def test_csv_and_image(self):
        csv = self.root / "table.csv"
        csv.write_text('name,value\n"hello\nworld",2\n', encoding="utf-8")
        self.assertEqual(
            self.ingest(csv)["manifest"]["validation"]["row_count"], 2
        )
        image = self.root / "scan.png"
        Image.new("RGB", (40, 50)).save(image)
        details = self.ingest(image)["manifest"]["validation"]
        self.assertEqual((details["width"], details["height"]), (40, 50))
        image.write_bytes(image.read_bytes()[:30])
        with self.assertRaises(IngestionError):
            self.ingest(image)

    def test_binary_and_unknown_files(self):
        path = self.root / "binary.txt"
        path.write_bytes(b"MZ\x00\x01\x02")
        with self.assertRaises(IngestionError):
            self.ingest(path)
        path = self.root / "notes.custom"
        path.write_text("plain text", encoding="utf-8")
        with self.assertRaises(IngestionError):
            self.ingest(path)
        self.assertEqual(
            self.ingest(path, format_override="text")["manifest"]["format"],
            "text",
        )

    def test_invalid_office_and_dtd(self):
        path = self.root / "fake.docx"
        path.write_text("not a docx", encoding="utf-8")
        with self.assertRaises(IngestionError):
            self.ingest(path)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("wrong.xml", "<root/>")
        with self.assertRaises(IngestionError):
            self.ingest(path)
        path = self.root / "entities.xml"
        path.write_text(
            '<!DOCTYPE root [<!ENTITY x "expanded">]><root>&x;</root>',
            encoding="utf-8",
        )
        with self.assertRaises(IngestionError):
            self.ingest(path)

    def test_legacy_pdf_manifest_remains_unchanged(self):
        path = self.root / "old.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=10, height=10)
        with path.open("wb") as stream:
            writer.write(stream)
        first = self.ingest(path)
        manifest_path = Path(first["version_path"]) / "manifest.json"
        manifest = first["manifest"]
        manifest["schema_version"] = 1
        for name in ("format", "source_kind", "validation"):
            del manifest[name]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before = manifest_path.read_bytes()
        self.assertTrue(self.ingest(path)["duplicate"])
        self.assertEqual(before, manifest_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
