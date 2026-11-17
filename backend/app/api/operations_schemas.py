from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.api.schemas import ArticleResponse


class OperationsControlResponse(BaseModel):
    publishing_paused: bool
    collection_paused: bool
    publish_mode: str
    auto_publish_enabled: bool
    max_posts_per_day: int
    posts_published_today: int
    pending_review_count: int
    latest_collection_status: str | None
    latest_errors: list[dict[str, str]]
    reason: str | None
    updated_by: str
    updated_at: datetime | None


class PauseRequest(BaseModel):
    scope: Literal["publishing", "collection", "all"] = "all"
    reason: str = Field(min_length=1, max_length=500)


class ReviewQueueResponse(BaseModel):
    articles: list[ArticleResponse]


class DailySummaryResponse(BaseModel):
    day: date
    collection_runs: int
    articles_discovered: int
    approved: int
    rejected: int
    published: int
    failed_deliveries: int
    pending_review: int


class AutoPublishOutcome(BaseModel):
    draft_id: str
    status: str
    detail: str | None = None


class AutoPublishResponse(BaseModel):
    attempted: int
    published: int
    outcomes: list[AutoPublishOutcome]
