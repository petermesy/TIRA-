import sqlalchemy as sa
from alembic import op

revision = "0003_v2_verification"
down_revision = "0002_v1_collection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_credibility",
        sa.Column("domain", sa.String(length=255), primary_key=True),
        sa.Column("tier", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "verification_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "article_id",
            sa.String(length=36),
            sa.ForeignKey("articles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("verification_score", sa.Float(precision=53), nullable=False),
        sa.Column("importance_score", sa.Float(precision=53), nullable=False),
        sa.Column("importance_inputs", sa.JSON(), nullable=False),
        sa.Column("importance_weights", sa.JSON(), nullable=False),
        sa.Column("importance_policy_version", sa.String(length=50), nullable=False),
        sa.Column("score_factors", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("cve_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("article_id", "version", name="uq_verification_article_version"),
    )
    op.create_index("ix_verification_runs_article_id", "verification_runs", ["article_id"])
    op.create_table(
        "verified_claims",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "verification_run_id",
            sa.String(length=36),
            sa.ForeignKey("verification_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claim_key", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_material", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("verification_score", sa.Float(precision=53), nullable=False),
        sa.Column("score_factors", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("search_queries", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("verification_run_id", "claim_key", name="uq_claim_run_key"),
    )
    op.create_index(
        "ix_verified_claims_verification_run_id",
        "verified_claims",
        ["verification_run_id"],
    )
    op.create_table(
        "claim_evidence",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "claim_id",
            sa.String(length=36),
            sa.ForeignKey("verified_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("publisher", sa.String(length=255), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("evidence_type", sa.String(length=32), nullable=False),
        sa.Column("stance", sa.String(length=16), nullable=False),
        sa.Column("is_materially_misleading", sa.Boolean(), nullable=False),
        sa.Column("validated_cve_ids", sa.JSON(), nullable=False),
        sa.Column("independence_group", sa.String(length=255), nullable=False),
        sa.Column("source_tier", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("claim_id", "url", name="uq_evidence_claim_url"),
    )
    op.create_index("ix_claim_evidence_claim_id", "claim_evidence", ["claim_id"])


def downgrade() -> None:
    op.drop_index("ix_claim_evidence_claim_id", table_name="claim_evidence")
    op.drop_table("claim_evidence")
    op.drop_index("ix_verified_claims_verification_run_id", table_name="verified_claims")
    op.drop_table("verified_claims")
    op.drop_index("ix_verification_runs_article_id", table_name="verification_runs")
    op.drop_table("verification_runs")
    op.drop_table("source_credibility")
