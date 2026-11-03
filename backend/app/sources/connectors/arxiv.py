from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.core.config import Settings
from app.core.security import canonicalize_url
from app.database.models import Source
from app.sources.connectors.contracts import ArticleCandidate, ConnectorError, SourceBatch
from app.sources.connectors.http import safe_get
from app.sources.connectors.rss import ATOM_NS, _child_text, _parse_date


class ArxivConnector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch(self, source: Source, cursor: str | None, limit: int) -> SourceBatch:
        if not source.base_url:
            raise ConnectorError("arXiv API URL is missing", error_class="InvalidSource")
        parsed = urlsplit(source.base_url)
        if parsed.hostname not in {"export.arxiv.org", "arxiv.org"}:
            raise ConnectorError(
                "arXiv connector only supports official arXiv hosts", error_class="InvalidSource"
            )
        response, requests_made = await safe_get(
            source.base_url,
            self.settings,
            rate_limit_per_minute=source.rate_limit_per_minute,
        )
        try:
            root = fromstring(response.content)
        except (ParseError, DefusedXmlException) as exc:
            raise ConnectorError(
                "arXiv returned malformed XML", error_class="MalformedResponse"
            ) from exc

        items: list[ArticleCandidate] = []
        warnings: list[str] = []
        for entry in root.findall(f"{ATOM_NS}entry")[:limit]:
            title = " ".join(_child_text(entry, f"{ATOM_NS}title").split())
            abstract = " ".join(_child_text(entry, f"{ATOM_NS}summary").split())
            identifier = _child_text(entry, f"{ATOM_NS}id")
            if not title or not identifier:
                warnings.append("Skipped arXiv record missing title or identifier")
                continue
            canonical_url = canonicalize_url(identifier.replace("http://", "https://", 1))
            author = ", ".join(
                name.text.strip()
                for name in entry.findall(f"{ATOM_NS}author/{ATOM_NS}name")
                if name.text and name.text.strip()
            )
            items.append(
                ArticleCandidate(
                    title=title[:500],
                    url=canonical_url,
                    source_name=source.name,
                    source_domain=urlsplit(canonical_url).hostname or "arxiv.org",
                    published_at=_parse_date(
                        _child_text(entry, f"{ATOM_NS}published", f"{ATOM_NS}updated")
                    ),
                    author=author[:255] or None,
                    content_excerpt=abstract[: self.settings.max_article_chars],
                    language="en",
                    retrieval_method="API",
                )
            )
        return SourceBatch(items=items, warnings=warnings, requests_made=requests_made)
