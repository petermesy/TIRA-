from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import canonicalize_url
from app.database.models import Source
from app.sources.connectors.contracts import ArticleCandidate, ConnectorError, SourceBatch
from app.sources.connectors.http import safe_get


class GitHubReleasesConnector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch(self, source: Source, cursor: str | None, limit: int) -> SourceBatch:
        if not source.base_url or urlsplit(source.base_url).hostname != "api.github.com":
            raise ConnectorError(
                "GitHub releases connector requires an api.github.com URL",
                error_class="InvalidSource",
            )
        parsed = urlsplit(source.base_url)
        segments = [part for part in parsed.path.split("/") if part]
        if len(segments) != 4 or segments[0] != "repos" or segments[3] != "releases":
            raise ConnectorError(
                "GitHub URL must be /repos/{owner}/{repo}/releases",
                error_class="InvalidSource",
            )
        response, requests_made = await safe_get(
            source.base_url,
            self.settings,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            rate_limit_per_minute=source.rate_limit_per_minute,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise ConnectorError(
                "GitHub returned invalid JSON", error_class="MalformedResponse"
            ) from exc
        if not isinstance(data, list):
            raise ConnectorError(
                "GitHub returned an unexpected response", error_class="MalformedResponse"
            )

        items: list[ArticleCandidate] = []
        warnings: list[str] = []
        for release in data[:limit]:
            if not isinstance(release, dict):
                warnings.append("Skipped malformed GitHub release")
                continue
            title = release.get("name") or release.get("tag_name")
            html_url = release.get("html_url")
            if not isinstance(title, str) or not isinstance(html_url, str):
                warnings.append("Skipped GitHub release missing title or URL")
                continue
            published = release.get("published_at")
            published_at = None
            if isinstance(published, str):
                try:
                    published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if published_at.tzinfo is None:
                        published_at = published_at.replace(tzinfo=UTC)
                except ValueError:
                    warnings.append("GitHub release had an invalid publication date")
            canonical_url = canonicalize_url(html_url)
            excerpt = release.get("body") if isinstance(release.get("body"), str) else ""
            try:
                items.append(
                    ArticleCandidate(
                        title=title[:500],
                        url=canonical_url,
                        source_name=source.name,
                        source_domain=urlsplit(canonical_url).hostname or "github.com",
                        published_at=published_at,
                        author=None,
                        content_excerpt=excerpt[: self.settings.max_article_chars],
                        language="en",
                        retrieval_method="API",
                    )
                )
            except ValidationError:
                warnings.append("Skipped malformed GitHub release")
        return SourceBatch(items=items, warnings=warnings, requests_made=requests_made)
