import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element, ParseError

from bs4 import BeautifulSoup
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.core.config import Settings
from app.core.security import canonicalize_url, validate_public_url
from app.database.models import Source
from app.sources.connectors.contracts import (
    ArticleCandidate,
    ConnectorError,
    SourceBatch,
)
from app.sources.connectors.http import safe_get

CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"


def _child_text(element: Element, *names: str) -> str:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _plain_text(value: str) -> str:
    return re.sub(
        r"\s+", " ", BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    ).strip()


def _image_url(entry: Element) -> str | None:
    enclosure = entry.find("enclosure")
    if enclosure is not None and (enclosure.get("type") or "").lower().startswith("image/"):
        return enclosure.get("url")

    media_content = entry.find(f"{MEDIA_NS}content")
    if media_content is not None and (
        media_content.get("medium") == "image"
        or (media_content.get("type") or "").lower().startswith("image/")
    ):
        return media_content.get("url")

    thumbnail = entry.find(f"{MEDIA_NS}thumbnail")
    if thumbnail is not None:
        return thumbnail.get("url")

    atom_link = entry.find(f"{ATOM_NS}link[@rel='enclosure']")
    if atom_link is not None and (atom_link.get("type") or "").lower().startswith("image/"):
        return atom_link.get("href")
    return None


class RSSConnector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def fetch(self, source: Source, cursor: str | None, limit: int) -> SourceBatch:
        if not source.base_url:
            raise ConnectorError("RSS source URL is missing", error_class="InvalidSource")
        response, requests_made = await safe_get(
            source.base_url,
            self.settings,
            headers={
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"
            },
            rate_limit_per_minute=source.rate_limit_per_minute,
        )
        try:
            root = fromstring(response.content)
        except (ParseError, DefusedXmlException) as exc:
            raise ConnectorError(
                "RSS/Atom source returned malformed XML", error_class="MalformedFeed"
            ) from exc

        source_name = source.name
        channel_title = root.find("./channel/title")
        if channel_title is not None and channel_title.text:
            source_name = channel_title.text.strip()[:255]
        entries = root.findall("./channel/item")
        if not entries:
            entries = root.findall(f"{ATOM_NS}entry")
            source_name = _child_text(root, f"{ATOM_NS}title")[:255] or source_name

        items: list[ArticleCandidate] = []
        warnings: list[str] = []
        for entry in entries[: max(limit * 3, limit)]:
            title = _child_text(entry, "title", f"{ATOM_NS}title")
            link = _child_text(entry, "link")
            if not link:
                atom_link = entry.find(f"{ATOM_NS}link")
                if atom_link is not None:
                    link = atom_link.get("href", "")
            if not title or not link:
                warnings.append("Skipped feed record missing a title or link")
                continue
            try:
                canonical_url = canonicalize_url(link)
            except ValueError:
                warnings.append("Skipped feed record with an invalid URL")
                continue
            excerpt = _child_text(
                entry,
                CONTENT_NS,
                "description",
                "summary",
                f"{ATOM_NS}summary",
                f"{ATOM_NS}content",
            )
            published = _parse_date(
                _child_text(
                    entry,
                    "pubDate",
                    "published",
                    "updated",
                    f"{ATOM_NS}published",
                    f"{ATOM_NS}updated",
                )
            )
            author = _child_text(entry, "author", f"{DC_NS}creator")
            if not author:
                author = _child_text(entry, f"{ATOM_NS}author/{ATOM_NS}name")
            image_url = _image_url(entry)
            if image_url:
                try:
                    image_url = await validate_public_url(urljoin(canonical_url, image_url))
                except ValueError:
                    warnings.append("Ignored an unsafe image URL from an RSS item")
                    image_url = None
                if image_url and urlsplit(image_url).scheme != "https":
                    warnings.append("Ignored a non-HTTPS image URL from an RSS item")
                    image_url = None
            try:
                items.append(
                    ArticleCandidate(
                        title=title[:500],
                        url=canonical_url,
                        source_name=source_name,
                        source_domain=urlsplit(canonical_url).hostname or source.domain or "",
                        published_at=published,
                        author=author[:255] if author else None,
                        image_url=image_url,
                        content_excerpt=_plain_text(excerpt)[: self.settings.max_article_chars],
                        language="en",
                        retrieval_method="RSS",
                    )
                )
            except ValueError:
                warnings.append("Skipped malformed feed record")
            if len(items) >= limit:
                break
        return SourceBatch(items=items, warnings=warnings, requests_made=requests_made)
