import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from openpyxl import Workbook
from PIL import Image
from pptx import Presentation
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from rag_pipeline.ingest import ingest_document
from rag_pipeline.render import RenderError, find_office, render_all, render_version


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def ingest(self, path):
        return Path(ingest_document(path, "example", self.root / "store")["version_path"])

    def pdf(self):
        path = self.root / "test.pdf"
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=200)
        stream = DecodedStreamObject()
        stream.set_data(b"1 0 0 rg 0 0 100 200 re f")
        page[NameObject("/Contents")] = writer._add_object(stream)
        page.rotate(90)
        writer.add_blank_page(width=100, height=200)
        with path.open("wb") as output:
            writer.write(output)
        return path

    def test_pdf_all_pages_rotation_pixels_and_cache(self):
        version = self.ingest(self.pdf())
        source_manifest = (version / "manifest.json").read_bytes()
        result = render_version(version, dpi=72)
        manifest = result["manifest"]
        self.assertEqual(manifest["page_count"], 2)
        self.assertEqual(manifest["visual_review"], "pending")
        with Image.open(Path(result["rendition_path"]) / "pages/page_0001.png") as image:
            self.assertEqual(image.size, (200, 100))
            self.assertEqual(image.convert("RGB").getpixel((50, 50)), (255, 0, 0))
        self.assertTrue(render_version(version, dpi=72)["cached"])
        self.assertNotEqual(result["rendition_path"], render_version(version, dpi=100)["rendition_path"])
        self.assertEqual(source_manifest, (version / "manifest.json").read_bytes())

    def test_text_escaping_encoding_and_line_anchors(self):
        path = self.root / "legacy.html"
        path.write_bytes('<script>alert("Привет")</script>\nsecond\n'.encode("cp1251"))
        version = Path(ingest_document(path, "text", self.root / "store", encoding="cp1251")["version_path"])
        result = render_version(version)
        preview = (Path(result["rendition_path"]) / "index.html").read_text(encoding="utf-8")
        self.assertIn('&lt;script&gt;', preview)
        self.assertNotIn('<script>', preview)
        self.assertIn('id="L2"', preview)
        self.assertIn('Привет', preview)
        self.assertEqual(result["manifest"]["line_count"], 2)
        self.assertIsNone(result["manifest"]["page_count"])

    def test_image_all_frames(self):
        path = self.root / "scan.tiff"
        Image.new("RGB", (20, 30), "red").save(path, save_all=True, append_images=[Image.new("RGB", (40, 50), "blue")])
        result = render_version(self.ingest(path))
        self.assertEqual(result["manifest"]["frame_count"], 2)
        self.assertEqual(result["manifest"]["items"][1]["width"], 40)
        self.assertIsNone(result["manifest"]["page_count"])

    def test_corrupt_cache_and_source_are_rejected(self):
        version = self.ingest(self.pdf())
        result = render_version(version, 72)
        (Path(result["rendition_path"]) / "pages/page_0001.png").write_bytes(b"broken")
        with self.assertRaisesRegex(RenderError, "damaged"):
            render_version(version, 72)
        (version / "original.pdf").write_bytes(b"changed")
        with self.assertRaisesRegex(RenderError, "SHA-256"):
            render_version(version, 72)

    def test_failure_does_not_publish_partial_result(self):
        version = self.ingest(self.pdf())
        with patch("rag_pipeline.render.MAX_PIXELS", 1):
            with self.assertRaises(RenderError):
                render_version(version, 72)
        self.assertEqual(list((version / "renders").iterdir()), [])
        self.assertFalse((version / ".render.lock").exists())

    def test_manifest_path_escape_rejected(self):
        version = self.ingest(self.pdf())
        manifest_path = version / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["original_path"] = "../outside.pdf"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RenderError, "escapes"):
            render_version(version)

    def test_missing_office_is_actionable(self):
        with self.assertRaisesRegex(RenderError, "requires LibreOffice"):
            find_office(str(self.root / "missing.exe"))

    def test_batch_reports_partial_failure_without_losing_success(self):
        version = self.ingest(self.pdf())
        broken = self.root / "store/broken/version"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("{}")
        result = render_all(self.root / "store", dpi=72)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["total_pdf_pages"], 2)
        self.assertEqual(result["versions_checked"], 2)
        self.assertTrue((self.root / "store/render-index.html").exists())
        self.assertTrue(list((version / "renders").glob("*/render_manifest.json")))

    def test_office_conversion_integration(self):
        try:
            executable = find_office()
        except RenderError:
            self.skipTest("LibreOffice not installed")
        docx = self.root / "manual.docx"
        doc = Document()
        doc.add_heading("Rendering checkpoint", 0)
        doc.add_paragraph("Проверка кириллицы, таблицы и изображения.")
        doc.add_table(rows=2, cols=2).cell(0, 0).text = "Example"
        picture = self.root / "picture.png"
        Image.new("RGB", (40, 20), "blue").save(picture)
        doc.add_picture(str(picture))
        doc.save(docx)
        xlsx = self.root / "table.xlsx"
        workbook = Workbook()
        workbook.active["A1"] = "Sheet content"
        workbook.save(xlsx)
        workbook.close()
        pptx = self.root / "slides.pptx"
        presentation = Presentation()
        presentation.slides.add_slide(presentation.slide_layouts[0]).shapes.title.text = "Slide content"
        presentation.save(pptx)
        for path in (docx, xlsx, pptx):
            with self.subTest(format=path.suffix):
                result = render_version(self.ingest(path), dpi=72, soffice=str(executable))
                self.assertGreater(result["manifest"]["page_count"], 0)
                self.assertTrue((Path(result["rendition_path"]) / "document.pdf").is_file())


if __name__ == "__main__":
    unittest.main()
