import re
from urllib.parse import urlsplit

from app.core.config import Settings

UNSUPPORTED_SUPERLATIVES = {
    "revolutionary",
    "unprecedented",
    "game-changing",
    "world's first",
    "world’s first",
    "guaranteed",
}


def check_draft(
    *,
    body: str,
    source_url: str | None,
    source_name: str,
    source_text: str,
    settings: Settings,
) -> list[str]:
    findings: list[str] = []
    if len(body) > settings.max_telegram_message_length:
        findings.append("Draft exceeds the configured Telegram message length")

    if not source_name.strip() or not ("Source:" in body or "Sources:" in body):
        findings.append("Draft is missing source attribution")
    if source_url:
        try:
            parts = urlsplit(source_url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or source_url not in body
            ):
                findings.append("Draft is missing a valid direct source link")
        except ValueError:
            findings.append("Draft is missing a valid direct source link")

    source_lower = source_text.lower()
    body_lower = body.lower()
    for phrase in UNSUPPORTED_SUPERLATIVES:
        if phrase in body_lower and phrase not in source_lower:
            findings.append(f"Unsupported superlative requires reviewer edit: {phrase}")

    source_numbers = set(
        re.findall(r"\b(?:CVE-\d{4}-\d+|\d+(?:\.\d+)?%?)\b", source_text, re.IGNORECASE)
    )
    for number in set(re.findall(r"\b(?:CVE-\d{4}-\d+|\d+(?:\.\d+)?%?)\b", body, re.IGNORECASE)):
        if number not in source_numbers:
            findings.append(f"Draft contains a numeric claim absent from the source: {number}")
    return findings
