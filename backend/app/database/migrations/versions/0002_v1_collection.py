import sqlalchemy as sa
from alembic import op

revision = "0002_v1_collection"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("categories", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "sources",
        sa.Column("max_items_per_run", sa.Integer(), nullable=False, server_default="20"),
    )
    op.add_column(
        "sources",
        sa.Column("interval_minutes", sa.Integer(), nullable=False, server_default="60"),
    )
    op.add_column(
        "sources",
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="30"),
    )
    op.add_column("sources", sa.Column("last_checked_at", sa.DateTime(timezone=True)))
    op.add_column("sources", sa.Column("last_success_at", sa.DateTime(timezone=True)))
    op.add_column(
        "sources",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("sources", sa.Column("last_error", sa.Text()))
    op.create_table(
        "collection_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sources_checked", sa.Integer(), nullable=False),
        sa.Column("items_found", sa.Integer(), nullable=False),
        sa.Column("items_new", sa.Integer(), nullable=False),
        sa.Column("duplicates", sa.Integer(), nullable=False),
        sa.Column("filtered", sa.Integer(), nullable=False),
        sa.Column("items_analyzed", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("source_metrics", sa.JSON(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("collection_runs")
    op.drop_column("sources", "last_error")
    op.drop_column("sources", "consecutive_failures")
    op.drop_column("sources", "last_success_at")
    op.drop_column("sources", "last_checked_at")
    op.drop_column("sources", "interval_minutes")
    op.drop_column("sources", "rate_limit_per_minute")
    op.drop_column("sources", "max_items_per_run")
    op.drop_column("sources", "categories")
