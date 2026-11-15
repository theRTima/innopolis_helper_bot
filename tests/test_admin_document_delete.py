"""Admin document deletion and re-upload deduplication behavior."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.admin.documents import (
    checksum_already_exists,
    delete_document_records,
    remove_document_files,
)
from app.storage.models import (
    Base,
    Chunk,
    Document,
    DocumentImportJob,
    DocumentVersion,
    ImportRun,
)

CHECKSUM = "a" * 64


def test_delete_removes_document_but_keeps_upload_history(tmp_path):
    async def scenario():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'admin.sqlite3'}"
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        version_dir = (
            tmp_path / "documents" / "upload-abc" / "sha256-version"
        )
        version_dir.mkdir(parents=True)
        (version_dir / "original.pdf").write_bytes(b"%PDF-1.7 test")

        async with sessions() as session, session.begin():
            document = Document(
                slug="upload-abc",
                title="Travel policy",
                format="pdf",
                active_version=1,
            )
            session.add(document)
            await session.flush()
            version = DocumentVersion(
                document_id=document.id,
                version=1,
                checksum_sha256=CHECKSUM,
                storage_path=str(version_dir),
            )
            session.add(version)
            await session.flush()
            session.add(
                Chunk(
                    document_version_id=version.id,
                    ordinal=0,
                    text="policy text",
                    locator_kind="page",
                    locator_value="1",
                )
            )
            session.add(
                ImportRun(
                    document_id=document.id,
                    document_version_id=version.id,
                    parser="docling+vision",
                    imported=True,
                    chunk_count=1,
                    parse_ms=1,
                    total_ms=1,
                )
            )
            session.add(
                DocumentImportJob(
                    document_id="upload-abc",
                    title="Travel policy",
                    original_name="travel.pdf",
                    storage_path="",
                    checksum_sha256=CHECKSUM,
                    status="completed",
                    progress_percent=100,
                    progress_stage="completed",
                )
            )

        async with sessions() as session:
            assert await checksum_already_exists(session, CHECKSUM)

        async with sessions() as session, session.begin():
            deleted = await delete_document_records(session, "upload-abc")
        assert deleted == ("Travel policy", [str(version_dir)])

        removed, failed = remove_document_files(
            deleted[1], tmp_path / "documents"
        )
        assert removed == [str(version_dir)]
        assert failed == []
        assert not version_dir.exists()
        assert not (tmp_path / "documents" / "upload-abc").exists()

        async with sessions() as session:
            assert not await checksum_already_exists(session, CHECKSUM)
            assert await session.scalar(select(func.count(Document.id))) == 0
            assert (
                await session.scalar(select(func.count(DocumentVersion.id)))
                == 0
            )
            assert await session.scalar(select(func.count(Chunk.id))) == 0
            assert await session.scalar(select(func.count(ImportRun.id))) == 0
            jobs = list(await session.scalars(select(DocumentImportJob)))
            assert len(jobs) == 1
            assert jobs[0].checksum_sha256 == CHECKSUM
            assert jobs[0].status == "completed"

        await engine.dispose()

    asyncio.run(scenario())


def test_delete_never_removes_paths_outside_document_root(tmp_path):
    documents_root = tmp_path / "documents"
    version_dir = documents_root / "upload-abc" / "sha256-version"
    version_dir.mkdir(parents=True)
    outside = tmp_path / "outside" / "keep.txt"
    outside.parent.mkdir()
    outside.write_text("keep", encoding="utf-8")

    removed, failed = remove_document_files(
        [str(version_dir), str(outside)], documents_root
    )
    assert removed == [str(version_dir)]
    assert failed == [str(outside)]
    assert outside.is_file()
    assert not version_dir.exists()


def test_completed_history_alone_does_not_block_reupload(tmp_path):
    async def scenario():
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'dedupe.sqlite3'}"
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async with sessions() as session, session.begin():
            session.add(
                DocumentImportJob(
                    document_id="upload-old",
                    title="Old upload",
                    original_name="old.pdf",
                    storage_path="",
                    checksum_sha256=CHECKSUM,
                    status="completed",
                    progress_percent=100,
                    progress_stage="completed",
                )
            )
        async with sessions() as session:
            assert not await checksum_already_exists(session, CHECKSUM)

        async with sessions() as session, session.begin():
            session.add(
                DocumentImportJob(
                    document_id="upload-new",
                    title="New upload",
                    original_name="new.pdf",
                    storage_path="",
                    checksum_sha256=CHECKSUM,
                )
            )
        async with sessions() as session:
            assert await checksum_already_exists(session, CHECKSUM)

        await engine.dispose()

    asyncio.run(scenario())
