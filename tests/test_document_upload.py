"""Admin upload route stores the file and enqueues parsing without blocking."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytest.importorskip("python_multipart")

from app.admin.documents import register_document_routes
from app.storage.models import Document, DocumentImportJob, DocumentVersion


def test_upload_creates_queued_job_and_stages_file(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'uploads.sqlite3'}")
        async with engine.begin() as connection:
            await connection.run_sync(Document.__table__.create)
            await connection.run_sync(DocumentVersion.__table__.create)
            await connection.run_sync(DocumentImportJob.__table__.create)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        app = FastAPI()
        inbox = tmp_path / "inbox"
        register_document_routes(app, sessions=sessions, inbox=inbox)

        with TestClient(app) as client:
            response = client.post(
                "/documents/upload",
                data={"title": "Furniture registry"},
                files={"document": ("registry.pdf", b"%PDF-1.7 test", "application/pdf")},
                follow_redirects=False,
            )
            duplicate = client.post(
                "/documents/upload",
                data={"title": "Furniture registry copy"},
                files={"document": ("registry-copy.pdf", b"%PDF-1.7 test", "application/pdf")},
                follow_redirects=False,
            )
        assert response.status_code == 303
        assert duplicate.status_code == 303
        assert "Same%20file%20already%20exists" in duplicate.headers["location"]
        async with sessions() as session:
            jobs = list((await session.scalars(select(DocumentImportJob))).all())
            assert len(jobs) == 2
            job = next(item for item in jobs if item.status == "queued")
            duplicate_job = next(
                item for item in jobs if item.progress_stage == "file already exists"
            )
            assert job.title == "Furniture registry"
            assert job.original_name == "registry.pdf"
            assert job.status == "queued"
            assert job.checksum_sha256
            assert job.progress_percent == 0
            assert duplicate_job.status == "completed"
            assert duplicate_job.progress_percent == 100
            assert duplicate_job.checksum_sha256 == job.checksum_sha256
            assert duplicate_job.storage_path == ""
            staged = inbox / job.document_id / "registry.pdf"
            assert staged.read_bytes() == b"%PDF-1.7 test"
            assert len([p for p in inbox.iterdir() if p.is_dir()]) == 1
        await engine.dispose()

    asyncio.run(scenario())
