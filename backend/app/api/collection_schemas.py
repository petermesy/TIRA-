from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, model_validator


class SourceType(StrEnum):
    RSS = "RSS"
    ARXIV = "ARXIV"
    GITHUB_RELEASES = "GITHUB_RELEASES"


class SourceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    base_url: HttpUrl
    categories: list[str] = Field(default_factory=list, max_length=6)
    enabled: bool = True
    interval_minutes: int = Field(default=60, ge=15, le=10_080)
    rate_limit_per_minute: int = Field(default=30, ge=1, le=600)
    max_items_per_run: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_categories(self) -> "SourceCreateRequest":
        allowed = {"AI", "CYBERSECURITY", "SOFTWARE", "EMERGING_TECH", "SCIENCE"}
        invalid = set(self.categories) - allowed
        if invalid:
            raise ValueError(f"Unsupported source categories: {', '.join(sorted(invalid))}")
        return self


class SourceResponse(BaseModel):
    id: str
    name: str
    source_type: str
    base_url: str | None
    domain: str | None
    categories: list[str]
    enabled: bool
    interval_minutes: int
    rate_limit_per_minute: int
    max_items_per_run: int
    last_checked_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None


class SourceUpdateRequest(BaseModel):
    enabled: bool


class SourceRunMetrics(BaseModel):
    source_id: str
    source_name: str
    status: str
    items_found: int
    items_new: int
    duplicates: int
    filtered: int
    requests_made: int
    error_class: str | None = None


class CollectionRunResponse(BaseModel):
    id: str
    status: str
    trigger_type: str
    started_at: datetime
    finished_at: datetime | None
    sources_checked: int
    items_found: int
    items_new: int
    duplicates: int
    filtered: int
    items_analyzed: int
    duration_ms: int | None
    errors: list[dict[str, str]]
    source_metrics: list[dict[str, object]]


class CollectionStatusResponse(BaseModel):
    sources_enabled: int
    latest_run: CollectionRunResponse | None
