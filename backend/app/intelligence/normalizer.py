import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from app.api.schemas import Category


def normalize_text(text: str, max_chars: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        raise ValueError("Article text is empty after normalization")
    if len(normalized) > max_chars:
        raise ValueError(f"Article text exceeds the {max_chars}-character limit")
    return normalized


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_domain(url: str | None) -> str | None:
    if not url:
        return None
    return urlsplit(url).hostname


def classify_category(text: str, category_hint: Category | None = None) -> Category:
    if category_hint is not None:
        return category_hint
    lowered = text.lower()
    patterns = {
        Category.CYBERSECURITY: (
            "cve-",
            "vulnerability",
            "security advisory",
            "ransomware",
            "patch",
        ),
        Category.AI: (
            "artificial intelligence",
            "machine learning",
            "llm",
            "neural network",
            "ai model",
            "ai",
        ),
        Category.SOFTWARE: (
            "open source",
            "release",
            "programming language",
            "framework",
            "developer tool",
        ),
        Category.EMERGING_TECH: (
            "robotics",
            "semiconductor",
            "quantum",
            "cloud computing",
            "edge computing",
        ),
        Category.SCIENCE: (
            "research paper",
            "scientists",
            "study finds",
            "experiment",
            "peer-reviewed",
        ),
    }
    matches = [
        (
            sum(
                len(re.findall(rf"\b{re.escape(term)}\b", lowered))
                if len(term) <= 2
                else lowered.count(term)
                for term in terms
            ),
            category,
        )
        for category, terms in patterns.items()
    ]
    count, category = max(matches, default=(0, Category.OTHER), key=lambda item: item[0])
    return category if count else Category.OTHER


def utc_now() -> datetime:
    return datetime.now(UTC)
