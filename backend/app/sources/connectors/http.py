import asyncio
import time
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import Settings
from app.core.security import validate_public_url
from app.sources.connectors.contracts import ConnectorError

_HOST_REQUEST_LOCKS: dict[str, asyncio.Lock] = {}
_HOST_NEXT_REQUEST: dict[str, float] = {}


async def _wait_for_rate_slot(host: str, rate_limit_per_minute: int) -> None:
    lock = _HOST_REQUEST_LOCKS.setdefault(host, asyncio.Lock())
    interval = 60.0 / rate_limit_per_minute
    async with lock:
        now = time.monotonic()
        next_allowed = _HOST_NEXT_REQUEST.get(host, now)
        wait_seconds = max(0.0, next_allowed - now)
        _HOST_NEXT_REQUEST[host] = max(now, next_allowed) + interval
    if wait_seconds:
        await asyncio.sleep(wait_seconds)


async def safe_get(
    url: str,
    settings: Settings,
    *,
    headers: dict[str, str] | None = None,
    rate_limit_per_minute: int = 30,
) -> tuple[httpx.Response, int]:
    try:
        current_url = await validate_public_url(url)
    except ValueError as exc:
        raise ConnectorError(
            "Source URL is not allowed",
            error_class="UnsafeSourceURL",
        ) from exc

    attempts = 0
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": "TIRA/0.1 (+public-interest technology research)",
            **(headers or {}),
        },
    ) as client:
        for redirect_count in range(4):
            for attempt in range(2):
                await _wait_for_rate_slot(
                    urlsplit(current_url).hostname or "",
                    rate_limit_per_minute,
                )
                attempts += 1
                try:
                    response = await client.get(current_url)
                except httpx.HTTPError as exc:
                    if attempt == 0:
                        await asyncio.sleep(0.1)
                        continue
                    raise ConnectorError(
                        f"Source request failed ({type(exc).__name__})",
                        error_class=type(exc).__name__,
                    ) from exc

                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or redirect_count == 3:
                        raise ConnectorError(
                            "Source exceeded redirect policy",
                            error_class="UnsafeRedirect",
                        )
                    try:
                        current_url = await validate_public_url(urljoin(current_url, location))
                    except ValueError as exc:
                        raise ConnectorError(
                            "Source redirected to an unsafe address",
                            error_class="UnsafeRedirect",
                        ) from exc
                    break

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 0:
                        await asyncio.sleep(0.1)
                        continue
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ConnectorError(
                        f"Source returned HTTP {response.status_code}",
                        error_class="HTTPStatusError",
                    ) from exc
                if len(response.content) > settings.max_fetch_bytes:
                    raise ConnectorError(
                        "Source response exceeds the configured byte limit",
                        error_class="ResponseTooLarge",
                    )
                return response, attempts
            else:
                raise ConnectorError(
                    "Source request failed after bounded retry",
                    error_class="RetryExhausted",
                )
        raise ConnectorError("Source exceeded redirect policy", error_class="UnsafeRedirect")
