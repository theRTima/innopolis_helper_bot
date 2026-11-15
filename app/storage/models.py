"""Relational schema for bot activity and versioned source documents."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Question(Base):
    """Existing question log, retained for local SQLite data and feedback buttons."""

    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String, server_default=func.current_timestamp())


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint("value IN ('helpful', 'not_helpful')", name="ck_feedback_value"),
        UniqueConstraint("question_id", "telegram_id", name="uq_feedback_question_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), nullable=False)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, server_default=func.current_timestamp())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("user_id", "chat_id", name="uq_conversation_user_chat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (CheckConstraint("role IN ('user', 'assistant', 'operator')", name="ck_messages_role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    # The bot retrieves chunks only from this version. Older versions remain available.
    active_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_document_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunks_sha256: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_version_id", "ordinal", name="uq_chunk_version_ordinal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # Parser-provided identifier, e.g. "document.pdf:p6:c0"; DB id remains the PK.
    external_id: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(500))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    # Structured parser output, such as table cells or normalized vision fields.
    data: Mapped[dict | list | None] = mapped_column(JSON)
    chunk_type: Mapped[str | None] = mapped_column(String(64))
    origin: Mapped[str | None] = mapped_column(String(64))
    locator_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    locator_value: Mapped[str] = mapped_column(String(255), nullable=False)


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), nullable=False)
    parser: Mapped[str] = mapped_column(String(32), nullable=False)
    imported: Mapped[bool] = mapped_column(nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    parse_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    vision_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), unique=True, nullable=False)
    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id"), unique=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Analitics(Base):
    __tablename__ = "analytics"
    __table_args__ = (
        CheckConstraint("answer_status IN ('with_sources', 'no_evidence', 'unverified', 'error') OR answer_status IS NULL", name="ck_analytics_answer_status"),
        CheckConstraint("operator_status IN ('requested', 'delivered', 'failed') OR operator_status IS NULL", name="ck_analytics_operator_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), unique=True, nullable=False)
    answer_status: Mapped[str | None] = mapped_column(String(32))
    operator_status: Mapped[str | None] = mapped_column(String(32))
    conversation_id: Mapped[int | None] = mapped_column(Integer)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str | None] = mapped_column(String(16))
    resolution: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())


class EscalationEvent(Base):
    __tablename__ = "escalation_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(ForeignKey("questions.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Citation(Base):
    __tablename__ = "citations"
    __table_args__ = (UniqueConstraint("answer_id", "position", name="uq_citation_answer_position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answers.id"), nullable=False, index=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
