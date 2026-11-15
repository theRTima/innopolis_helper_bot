"""Relational schema for bot activity and versioned source documents."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
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
    created_at: Mapped[str] = mapped_column(
        String, server_default=func.current_timestamp(), index=True
    )


class QuestionAnalytics(Base):
    """Optional analytics data for an existing question."""

    __tablename__ = "question_analytics"

    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True
    )
    area: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str | None] = mapped_column(String(32))
    issue: Mapped[str | None] = mapped_column(String(96))
    classified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    escalation_status: Mapped[str | None] = mapped_column(String(16))


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version", name="uq_document_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_version_id", "ordinal", name="uq_chunk_version_ordinal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id"), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locator_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    locator_value: Mapped[str] = mapped_column(String(255), nullable=False)


class Answer(Base):
    __tablename__ = "answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), unique=True, nullable=False)
    question_id: Mapped[int | None] = mapped_column(ForeignKey("questions.id"), unique=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Citation(Base):
    __tablename__ = "citations"
    __table_args__ = (UniqueConstraint("answer_id", "position", name="uq_citation_answer_position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    answer_id: Mapped[int] = mapped_column(ForeignKey("answers.id"), nullable=False, index=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
