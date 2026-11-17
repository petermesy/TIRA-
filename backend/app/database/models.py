from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid4())


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    canonical_url: Mapped[str | None] = mapped_column(String(2048), unique=True, nullable=True)
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    language: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    content_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    retrieval_method: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="DISCOVERED", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    analyses: Mapped[list["Analysis"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    drafts: Mapped[list["Draft"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    verification_runs: Mapped[list["VerificationRun"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("name", "source_type", name="uq_source_name_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    credibility_tier: Mapped[str] = mapped_column(String(32), default="UNASSESSED", nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    max_items_per_run: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sources_checked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    filtered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_analyzed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    source_metrics: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class OperationsControl(Base):
    __tablename__ = "operations_control"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="global")
    publishing_paused: Mapped[bool] = mapped_column(default=False, nullable=False)
    collection_paused: Mapped[bool] = mapped_column(default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class TelegramCallback(Base):
    __tablename__ = "telegram_callbacks"

    callback_query_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    reviewer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    draft_id: Mapped[str] = mapped_column(String(36), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class SourceCredibility(Base):
    __tablename__ = "source_credibility"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class VerificationRun(Base):
    __tablename__ = "verification_runs"
    __table_args__ = (
        UniqueConstraint("article_id", "version", name="uq_verification_article_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_score: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    importance_score: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    importance_inputs: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    importance_weights: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    importance_policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    score_factors: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    cve_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    article: Mapped[Article] = relationship(back_populates="verification_runs")
    claims: Mapped[list["VerifiedClaim"]] = relationship(
        back_populates="verification_run", cascade="all, delete-orphan"
    )


class VerifiedClaim(Base):
    __tablename__ = "verified_claims"
    __table_args__ = (
        UniqueConstraint("verification_run_id", "claim_key", name="uq_claim_run_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    verification_run_id: Mapped[str] = mapped_column(
        ForeignKey("verification_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    claim_key: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_material: Mapped[bool] = mapped_column(default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_score: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    score_factors: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    search_queries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    verification_run: Mapped[VerificationRun] = relationship(back_populates="claims")
    evidence: Mapped[list["ClaimEvidence"]] = relationship(
        back_populates="claim", cascade="all, delete-orphan"
    )


class ClaimEvidence(Base):
    __tablename__ = "claim_evidence"
    __table_args__ = (UniqueConstraint("claim_id", "url", name="uq_evidence_claim_url"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    claim_id: Mapped[str] = mapped_column(
        ForeignKey("verified_claims.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    publisher: Mapped[str] = mapped_column(String(255), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    stance: Mapped[str] = mapped_column(String(16), nullable=False)
    is_materially_misleading: Mapped[bool] = mapped_column(default=False, nullable=False)
    validated_cve_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    independence_group: Mapped[str] = mapped_column(String(255), nullable=False)
    source_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    claim: Mapped[VerifiedClaim] = relationship(back_populates="evidence")


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    generation_settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    relevance: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    novelty: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    impact: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    credibility: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    technical_significance: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    importance: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    rationale: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    evidence_references: Mapped[dict[str, list[str]]] = mapped_column(JSON, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    why_it_matters: Mapped[str | None] = mapped_column(Text, nullable=True)
    headline: Mapped[str] = mapped_column(String(500), nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    article: Mapped[Article] = relationship(back_populates="analyses")


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("article_id", "version", name="uq_draft_article_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    format_type: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    quality_status: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_findings: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    article: Mapped[Article] = relationship(back_populates="drafts")
    decisions: Mapped[list["ReviewDecision"]] = relationship(
        back_populates="draft", cascade="all, delete-orphan"
    )
    published_post: Mapped["PublishedPost | None"] = relationship(
        back_populates="draft", uselist=False, cascade="all, delete-orphan"
    )


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    draft: Mapped[Draft] = relationship(back_populates="decisions")


class PublishedPost(Base):
    __tablename__ = "published_posts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    channel_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retryable: Mapped[bool] = mapped_column(default=False, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    draft: Mapped[Draft] = relationship(back_populates="published_post")


class PostCorrection(Base):
    __tablename__ = "post_corrections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    original_post_id: Mapped[str] = mapped_column(
        ForeignKey("published_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    correction_draft_id: Mapped[str] = mapped_column(
        ForeignKey("drafts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    article_id: Mapped[str | None] = mapped_column(
        ForeignKey("articles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    draft_id: Mapped[str | None] = mapped_column(
        ForeignKey("drafts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), default="system", nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
