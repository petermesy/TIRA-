from app.core.config import Settings
from app.intelligence.normalizer import normalize_text


def read_manual_text(text: str, title: str | None, settings: Settings) -> tuple[str, str]:
    content = normalize_text(text, settings.max_article_chars)
    resolved_title = (
        title or next((line.strip() for line in text.splitlines() if line.strip()), "")
    )[:500]
    if not resolved_title:
        raise ValueError("A title is required when the submitted text has no non-empty line")
    return resolved_title, content
