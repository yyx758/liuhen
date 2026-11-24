"""文档管理与检索：documents、document_chunks、document_images、document_create_requests；
operation_events 支持文档事件；chat_turns 增加 sources。

降级会删除文档相关的审计事件（task_id 改回非空之前必须先删除它们）。

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_EVENT_TYPES = (
    "'task_created', 'task_updated', 'task_deleted', "
    "'proposal_created', 'proposal_replaced', "
    "'proposal_expired', 'proposal_executed', 'proposal_cancelled', "
    "'proposal_conflicted', 'proposal_failed', 'proposal_deleted'"
)
NEW_EVENT_TYPES = OLD_EVENT_TYPES + ", 'document_created', 'document_updated', 'document_deleted'"


def upgrade() -> None:
    # 片段检索用 trigram 索引（PRD D3）；官方 PostgreSQL 镜像自带这个 contrib 扩展。
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(btrim(title)) > 0", name="ck_documents_title_not_blank"),
        sa.CheckConstraint(
            "category IS NULL OR char_length(btrim(category)) > 0", name="ck_documents_category_not_blank"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_documents_user_id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_user_id_updated_at_id", "documents", ["user_id", "updated_at", "id"])
    op.create_index("ix_documents_user_id_category", "documents", ["user_id", "category"])
    op.create_index(
        "ix_documents_title_trgm", "documents", ["title"],
        postgresql_using="gin", postgresql_ops={"title": "gin_trgm_ops"},
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("section_seq", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.JSON(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("is_heading_only", sa.Boolean(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="ck_document_chunks_offsets"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name="fk_document_chunks_document_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_document_chunks_user_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "seq", name="uq_document_chunks_document_id_seq"),
    )
    op.create_index(
        "ix_document_chunks_document_id_section_seq", "document_chunks", ["document_id", "section_seq"]
    )
    op.create_index("ix_document_chunks_user_id", "document_chunks", ["user_id"])
    op.create_index(
        "ix_document_chunks_search_text_trgm", "document_chunks", ["search_text"],
        postgresql_using="gin", postgresql_ops={"search_text": "gin_trgm_ops"},
    )

    op.create_table(
        "document_images",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=16), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')",
            name="ck_document_images_content_type",
        ),
        sa.CheckConstraint("byte_size > 0", name="ck_document_images_byte_size"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_document_images_user_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_document_images_public_id"),
        sa.UniqueConstraint("user_id", "sha256", name="uq_document_images_user_id_sha256"),
    )
    op.create_index("ix_document_images_user_id_created_at", "document_images", ["user_id", "created_at"])

    op.create_table(
        "document_create_requests",
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("result_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_document_create_requests_user_id"),
        sa.PrimaryKeyConstraint("request_id"),
    )

    op.alter_column("operation_events", "task_id", existing_type=sa.Integer(), nullable=True)
    op.add_column("operation_events", sa.Column("document_id", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_operation_events_target", "operation_events", "(task_id IS NULL) <> (document_id IS NULL)"
    )
    op.drop_constraint("ck_operation_event_type", "operation_events", type_="check")
    op.create_check_constraint(
        "ck_operation_event_type", "operation_events", f"event_type IN ({NEW_EVENT_TYPES})"
    )
    op.create_index("ix_operation_events_document_id_id", "operation_events", ["document_id", "id"])

    op.add_column("chat_turns", sa.Column("sources", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_turns", "sources")

    op.execute("DELETE FROM operation_events WHERE document_id IS NOT NULL")
    op.drop_index("ix_operation_events_document_id_id", table_name="operation_events")
    op.drop_constraint("ck_operation_event_type", "operation_events", type_="check")
    op.create_check_constraint(
        "ck_operation_event_type", "operation_events", f"event_type IN ({OLD_EVENT_TYPES})"
    )
    op.drop_constraint("ck_operation_events_target", "operation_events", type_="check")
    op.drop_column("operation_events", "document_id")
    op.alter_column("operation_events", "task_id", existing_type=sa.Integer(), nullable=False)

    op.drop_table("document_create_requests")
    op.drop_index("ix_document_images_user_id_created_at", table_name="document_images")
    op.drop_table("document_images")
    op.drop_index("ix_document_chunks_search_text_trgm", table_name="document_chunks")
    op.drop_index("ix_document_chunks_user_id", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id_section_seq", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_index("ix_documents_title_trgm", table_name="documents")
    op.drop_index("ix_documents_user_id_category", table_name="documents")
    op.drop_index("ix_documents_user_id_updated_at_id", table_name="documents")
    op.drop_table("documents")
    # pg_trgm 扩展保留：其他数据库对象可能也在用。
