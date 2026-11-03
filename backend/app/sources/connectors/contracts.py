from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field, HttpUrl

from app.database.models import Source


class ArticleCandidate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    source_name: str = Field(min_length=1, max_length=255)
    source_domain: str = Field(min_length=1, max_length=255)
    published_at: datetime | None = None
    author: str | None = Field(default=None, max_length=255)
    content_excerpt: str = Field(default="", max_length=100_000)
    language: str = Field(default="en", min_length=2, max_length=16)
    retrieval_method: str


class SourceBatch(BaseModel):
    items: list[ArticleCandidate]
    next_cursor: str | None = None
    warnings: list[str] = Field(default_factory=list)
    requests_made: int = Field(default=1, ge=0)


class SourceConnector(Protocol):
    async def fetch(self, source: Source, cursor: str | None, limit: int) -> SourceBatch: ...


class ConnectorError(Exception):
    def __init__(self, message: str, *, error_class: str = "ConnectorError") -> None:
        super().__init__(message)
        self.error_class = error_class
