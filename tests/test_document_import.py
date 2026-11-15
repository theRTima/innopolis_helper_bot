"""Regression checks for parser revisions and vision result caching."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.ingest.service import import_document
from app.ingestion import vision_parser as vision
from app.storage.db import Database


class DocumentImportTests(unittest.TestCase):
    def test_same_pdf_with_changed_chunks_creates_new_version(self):
        async def check(root):
            pdf = root / "sample.pdf"
            pdf.write_bytes(b"sample")
            db = Database(root / "test.sqlite3")
            await db.init()
            try:
                base = {"page": 1, "text": "first", "content": "first"}
                async with db.session() as session, session.begin():
                    first, imported = await import_document(session, pdf, [base])
                    self.assertTrue(imported)
                    self.assertEqual(first.version, 1)
                async with db.session() as session, session.begin():
                    same, imported = await import_document(session, pdf, [base])
                    self.assertFalse(imported)
                    self.assertEqual(same.version, 1)
                changed = {**base, "text": "second", "content": "second"}
                async with db.session() as session, session.begin():
                    second, imported = await import_document(session, pdf, [changed])
                    self.assertTrue(imported)
                    self.assertEqual(second.version, 2)
            finally:
                await db.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(check(Path(directory)))

    def test_vision_cache_uses_image_and_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "sample.png"
            image.write_bytes(b"image A")
            with patch.object(vision, "VISION_CACHE_DIR", root / "cache"), patch.object(
                vision, "_chat_text", return_value='{"chunks": []}'
            ) as model:
                self.assertEqual(vision.run_vision("prompt A", image), '{"chunks": []}')
                self.assertEqual(vision.run_vision("prompt A", image), '{"chunks": []}')
                self.assertEqual(model.call_count, 1)
                vision.run_vision("prompt B", image)
                image.write_bytes(b"image B")
                vision.run_vision("prompt A", image)
                self.assertEqual(model.call_count, 3)
