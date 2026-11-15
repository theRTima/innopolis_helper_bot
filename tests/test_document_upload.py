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
from app.storage.models import DocumentImportJob


def test_upload_creates_queued_job_and_stages_file(tmp_path):
    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'uploads.sqlite3'}")
        async with engine.begin() as connection:
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
        assert response.status_code == 303
        async with sessions() as session:
            job = await session.scalar(select(DocumentImportJob))
            assert job is not None
            assert job.title == "Furniture registry"
            assert job.original_name == "registry.pdf"
            assert job.status == "queued"
            staged = inbox / job.document_id / "registry.pdf"
            assert staged.read_bytes() == b"%PDF-1.7 test"
        await engine.dispose()

    asyncio.run(scenario())
