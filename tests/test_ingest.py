import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfWriter

from rag_pipeline.ingest import IngestionError, ingest_pdf, sha256_file


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = self.root / "storage"
        self.pdf = self.root / "Командировка.pdf"
        self.make_pdf(self.pdf, 2)

    @staticmethod
    def make_pdf(path, pages, encrypted=False):
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=595, height=842)
        if encrypted:
            writer.encrypt("secret")
        with path.open("wb") as stream:
            writer.write(stream)

    def ingest(self, path=None, document_id="travel"):
        return ingest_pdf(path or self.pdf, document_id, self.storage)

    def test_snapshot_and_manifest(self):
        result = self.ingest()
        version = Path(result["version_path"])
        manifest = json.loads(
            (version / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertFalse(result["duplicate"])
        self.assertEqual(manifest["page_count"], 2)
        self.assertEqual(manifest["sha256"], sha256_file(self.pdf))
        self.assertEqual(manifest["original_filename"], self.pdf.name)
        self.assertEqual(
            (version / "original.pdf").read_bytes(), self.pdf.read_bytes()
        )
        self.assertEqual(list(version.parent.glob(".staging-*")), [])
        self.assertFalse((version.parent / ".ingest.lock").exists())

    def test_duplicate_even_if_renamed(self):
        first = self.ingest()
        renamed = self.root / "renamed.pdf"
        renamed.write_bytes(self.pdf.read_bytes())
        second = self.ingest(renamed)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["manifest"], second["manifest"])
        self.assertEqual(first["version_path"], second["version_path"])

    def test_changed_contents_create_new_version(self):
        first = self.ingest()
        self.make_pdf(self.pdf, 3)
        second = self.ingest()
        self.assertFalse(second["duplicate"])
        self.assertNotEqual(first["version_path"], second["version_path"])
        self.assertTrue(Path(first["version_path"]).exists())

    def test_invalid_inputs_never_publish_a_version(self):
        for content in (b"not a pdf", b"%PDF-1.7\ntruncated"):
            self.pdf.write_bytes(content)
            with self.assertRaises(IngestionError):
                self.ingest()
            self.assertEqual(list((self.storage / "travel").iterdir()), [])

    def test_encrypted_and_empty_rejected(self):
        for pages, encrypted in ((2, True), (0, False)):
            self.make_pdf(self.pdf, pages, encrypted)
            with self.assertRaises(IngestionError):
                self.ingest()

    def test_unsafe_id_and_missing_file(self):
        for value in ("../escape", "CON", "x/y", "", "TRAVEL"):
            with self.assertRaises(IngestionError):
                self.ingest(document_id=value)
        with self.assertRaises(IngestionError):
            self.ingest(self.root / "missing.pdf")

    def test_corrupt_stored_original_detected(self):
        result = self.ingest()
        (Path(result["version_path"]) / "original.pdf").write_bytes(b"broken")
        with self.assertRaisesRegex(IngestionError, "integrity"):
            self.ingest()

    def test_lock_prevents_parallel_writer(self):
        directory = self.storage / "travel"
        directory.mkdir(parents=True)
        lock = directory / ".ingest.lock"
        lock.write_text("another writer")
        with self.assertRaisesRegex(IngestionError, "locked"):
            self.ingest()
        self.assertTrue(lock.exists())


if __name__ == "__main__":
    unittest.main()
