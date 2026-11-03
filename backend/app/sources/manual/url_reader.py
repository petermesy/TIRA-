from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from app.core.config import Settings
from app.core.security import validate_public_url

ALLOWED_CONTENT_TYPES = {"text/html", "text/plain", "application/xhtml+xml"}
MAX_REDIRECTS = 3


class ArticleExtractionError(Exception):
    pass


@dataclass(frozen=True)
class ExtractedArticle:
    url: str
    title: str
    text: str
    source_name: str
    author: str | None
    published_at: datetime | None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _extract_document(url: str, content: bytes) -> ExtractedArticle:
    soup = BeautifulSoup(content, "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        element.decompose()
    title_element = soup.find("meta", attrs={"property": "og:title"}) or soup.find("title")
    title = ""
    if title_element:
        title = str(title_element.get("content") or title_element.get_text(" ", strip=True)).strip()
    if not title:
        title = (url.split("/")[-1] or "Untitled article")[:500]

    text = soup.get_text(" ", strip=True)
    author_meta = soup.find("meta", attrs={"name": "author"})
    author = (
        str(author_meta.get("content")).strip()
        if author_meta and author_meta.get("content")
        else None
    )
    published_meta = (
        soup.find("meta", attrs={"property": "article:published_time"})
        or soup.find("meta", attrs={"name": "date"})
        or soup.find("time")
    )
    date_value = None
    if published_meta:
        date_value = published_meta.get("content") or published_meta.get("datetime")
    return ExtractedArticle(
        url=url,
        title=title[:500],
        text=text,
        source_name=urlsplit(url).hostname or "Unknown source",
        author=author[:255] if author else None,
        published_at=_parse_datetime(str(date_value) if date_value else None),
    )


async def extract_url(url: str, settings: Settings) -> ExtractedArticle:
    try:
        current_url = await validate_public_url(url)
    except ValueError as exc:
        raise ArticleExtractionError(str(exc)) from exc

    headers = {"User-Agent": "TIRA/0.1 (+public-interest technology research)"}
    try:
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        ) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        if redirect_count == MAX_REDIRECTS:
                            raise ArticleExtractionError("URL exceeded the redirect limit")
                        location = response.headers.get("location")
                        if not location:
                            raise ArticleExtractionError(
                                "URL returned a redirect without a Location header"
                            )
                        try:
                            current_url = await validate_public_url(urljoin(current_url, location))
                        except ValueError as exc:
                            raise ArticleExtractionError(
                                f"Redirect target is not allowed: {exc}"
                            ) from exc
                        continue

                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type not in ALLOWED_CONTENT_TYPES:
                        raise ArticleExtractionError(
                            f"Unsupported article content type: {content_type or 'unknown'}"
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise ArticleExtractionError(
                                "Article response has an invalid Content-Length"
                            ) from exc
                        if declared_length > settings.max_fetch_bytes:
                            raise ArticleExtractionError(
                                "Article response exceeds the configured byte limit"
                            )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > settings.max_fetch_bytes:
                            raise ArticleExtractionError(
                                "Article response exceeds the configured byte limit"
                            )
                    article = _extract_document(current_url, bytes(body))
                    if not article.text.strip():
                        raise ArticleExtractionError(
                            "The page did not contain readable article text"
                        )
                    if len(article.text) > settings.max_article_chars:
                        article = ExtractedArticle(
                            url=article.url,
                            title=article.title,
                            text=article.text[: settings.max_article_chars],
                            source_name=article.source_name,
                            author=article.author,
                            published_at=article.published_at,
                        )
                    return article
    except ArticleExtractionError:
        raise
    except httpx.HTTPError as exc:
        raise ArticleExtractionError(
            f"Article URL could not be fetched ({type(exc).__name__})"
        ) from exc
    raise ArticleExtractionError("Article URL could not be fetched")
