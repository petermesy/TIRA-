from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Category(StrEnum):
    AI = "AI"
    CYBERSECURITY = "CYBERSECURITY"
    SOFTWARE = "SOFTWARE"
    EMERGING_TECH = "EMERGING_TECH"
    SCIENCE = "SCIENCE"
    OTHER = "OTHER"


class AnalyzeArticleRequest(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    url: str | None = Field(default=None, max_length=2048)
    text: str | None = Field(default=None, max_length=1_000_000)
    category_hint: Category | None = None
    language: str = Field(
        default="en",
        min_length=2,
        max_length=16,
        pattern=r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$",
    )

    @model_validator(mode="after")
    def require_exactly_one_content_source(self) -> "AnalyzeArticleRequest":
        if bool(self.url) == bool(self.text):
            raise ValueError("Provide exactly one of 'url' or 'text'")
        if self.text is not None and not self.text.strip():
            raise ValueError("Text must not be empty")
        return self


class ScoreBreakdown(BaseModel):
    relevance: float = Field(ge=0, le=10)
    novelty: float = Field(ge=0, le=10)
    impact: float = Field(ge=0, le=10)
    credibility: float = Field(ge=0, le=10)
    technical_significance: float = Field(ge=0, le=10)
    importance: float = Field(ge=0, le=10)


class AnalyzeArticleResponse(BaseModel):
    article_id: str
    draft_id: str
    category: Category
    scores: ScoreBreakdown
    recommendation: str
    quality_status: str
    status: str
    warnings: list[str] = Field(default_factory=list)


class AnalysisResponse(BaseModel):
    id: str
    provider: str
    model_name: str
    prompt_version: str
    generation_settings: dict[str, Any]
    scores: ScoreBreakdown
    rationale: dict[str, str]
    evidence_references: dict[str, list[str]]
    summary: str
    why_it_matters: str | None
    headline: str
    warnings: list[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DraftResponse(BaseModel):
    id: str
    article_id: str
    body: str
    format_type: str
    version: int
    quality_status: str
    quality_findings: list[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ArticleResponse(BaseModel):
    id: str
    canonical_url: str | None
    title: str
    source_name: str
    source_domain: str | None
    author: str | None
    published_at: datetime | None
    discovered_at: datetime
    category: Category
    language: str
    content_excerpt: str
    content_hash: str
    retrieval_method: str
    status: str
    analysis: AnalysisResponse | None
    current_draft: DraftResponse | None


class ReviewRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=2_000)


class EditDraftRequest(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)
    comment: str | None = Field(default=None, max_length=2_000)


class ReviewResponse(BaseModel):
    draft_id: str
    decision: str
    reviewer_id: str
    status: str


class DeliveryResponse(BaseModel):
    draft_id: str
    delivery_status: str
    channel_id: str
    message_id: str | None = None


class HealthResponse(BaseModel):
    status: str
    database: str
    llm_configured: bool
    telegram_configured: bool
    publish_mode: str


class ErrorResponse(BaseModel):
    detail: str
    context: dict[str, Any] | None = None
