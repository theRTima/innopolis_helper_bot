"""Keep existing bot logs and add versioned document and conversation tables."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "questions" not in existing:
        op.create_table(
            "questions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("chat_id", sa.BigInteger(), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("answer", sa.Text(), nullable=False),
            sa.Column("sources", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False, server_default=sa.func.current_timestamp()),
        )
    if "feedback" not in existing:
        op.create_table(
            "feedback",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("question_id", sa.Integer(), sa.ForeignKey("questions.id"), nullable=False),
            sa.Column("telegram_id", sa.BigInteger(), nullable=False),
            sa.Column("value", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.CheckConstraint("value IN ('helpful', 'not_helpful')", name="ck_feedback_value"),
            sa.UniqueConstraint("question_id", "telegram_id", name="uq_feedback_question_user"),
        )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("user_id", "chat_id", name="uq_conversation_user_chat"),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("telegram_message_id", sa.Integer()),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("role IN ('user', 'assistant', 'operator')", name="ck_messages_role"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
    )
    op.create_table(
        "document_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("document_id", "version", name="uq_document_version"),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])
    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_version_id", sa.Integer(), sa.ForeignKey("document_versions.id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("locator_kind", sa.String(32), nullable=False),
        sa.Column("locator_value", sa.String(255), nullable=False),
        sa.UniqueConstraint("document_version_id", "ordinal", name="uq_chunk_version_ordinal"),
    )
    op.create_index("ix_chunks_document_version_id", "chunks", ["document_version_id"])
    op.create_table(
        "answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False, unique=True),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("questions.id"), unique=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
    )
    op.create_table(
        "citations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("answer_id", sa.Integer(), sa.ForeignKey("answers.id"), nullable=False),
        sa.Column("chunk_id", sa.Integer(), sa.ForeignKey("chunks.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("answer_id", "position", name="uq_citation_answer_position"),
    )
    op.create_index("ix_citations_answer_id", "citations", ["answer_id"])
    op.create_index("ix_citations_chunk_id", "citations", ["chunk_id"])


def downgrade() -> None:
    # Existing questions/feedback may predate Alembic; never drop user logs here.
    for name in ("citations", "answers", "chunks", "document_versions", "documents", "messages", "conversations", "users"):
        op.drop_table(name)
