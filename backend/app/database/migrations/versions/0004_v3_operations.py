import sqlalchemy as sa
from alembic import op

revision = "0004_v3_operations"
down_revision = "0003_v2_verification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operations_control",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("publishing_paused", sa.Boolean(), nullable=False),
        sa.Column("collection_paused", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "telegram_callbacks",
        sa.Column("callback_query_id", sa.String(length=255), primary_key=True),
        sa.Column("reviewer_id", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "post_corrections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "original_post_id",
            sa.String(length=36),
            sa.ForeignKey("published_posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "correction_draft_id",
            sa.String(length=36),
            sa.ForeignKey("drafts.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_post_corrections_original_post_id",
        "post_corrections",
        ["original_post_id"],
    )
    op.add_column(
        "published_posts",
        sa.Column("response_metadata", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "published_posts",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "published_posts",
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "published_posts",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("published_posts", "next_retry_at")
    op.drop_column("published_posts", "retryable")
    op.drop_column("published_posts", "attempt_count")
    op.drop_column("published_posts", "response_metadata")
    op.drop_index("ix_post_corrections_original_post_id", table_name="post_corrections")
    op.drop_table("post_corrections")
    op.drop_table("telegram_callbacks")
    op.drop_table("operations_control")
