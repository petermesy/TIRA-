import asyncio
import hmac
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Header, HTTPException, Request, status

from app.core.config import Settings, get_settings

TRACKING_PARAMETERS = {"fbclid", "gclid", "dclid", "mc_cid", "mc_eid", "ref"}


def canonicalize_url(raw_url: str) -> str:
    parts = urlsplit(raw_url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL must use http or https and include a hostname")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs containing credentials are not allowed")

    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("URL contains an invalid hostname or port") from exc
    display_host = f"[{host}]" if ":" in host else host
    netloc = (
        display_host
        if port is None or (parts.scheme.lower(), port) in {("http", 80), ("https", 443)}
        else f"{display_host}:{port}"
    )
    path = parts.path or "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query, doseq=True), ""))


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return ip.is_global


async def validate_public_url(url: str) -> str:
    canonical = canonicalize_url(url)
    hostname = urlsplit(canonical).hostname
    if hostname is None:
        raise ValueError("URL has no hostname")
    try:
        literal_ip = ipaddress.ip_address(hostname)
        addresses = [str(literal_ip)]
    except ValueError:
        try:
            results = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    hostname,
                    None,
                    family=socket.AF_UNSPEC,
                    type=socket.SOCK_STREAM,
                ),
                timeout=3.0,
            )
        except (OSError, TimeoutError) as exc:
            raise ValueError("URL hostname could not be resolved safely") from exc
        addresses = list({result[4][0] for result in results})

    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise ValueError("URL resolves to a non-public network address")
    return canonical


async def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    settings: Settings = request.app.state.settings or get_settings()
    configured_token = settings.admin_api_token
    if configured_token is None or not configured_token.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin actions are disabled because ADMIN_API_TOKEN is not configured",
        )
    expected = f"Bearer {configured_token.get_secret_value()}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid admin bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return request.headers.get("x-reviewer-id", "admin")[:100]
