from app.api.schemas import Category


def build_draft(
    *,
    category: Category,
    summary: str,
    source_url: str | None,
    source_name: str,
) -> tuple[str, str]:
    source_line = (
        f"Source: {source_name} | {source_url}"
        if source_url
        else (f"Source: {source_name} (user-provided text; no source URL)")
    )
    if category == Category.CYBERSECURITY:
        title, tags, format_type = (
            "🔐 CYBERSECURITY UPDATE",
            "#Cybersecurity #Security",
            "cybersecurity",
        )
    elif category == Category.AI:
        title, tags, format_type = "🤖 AI UPDATE", "#AI #MachineLearning", "ai"
    elif category == Category.SCIENCE:
        title, tags, format_type = "🔬 SCIENCE FACT", "#ScienceFacts #Physics #Space", "research"
    elif category == Category.EMERGING_TECH:
        title = "🚀 TECHNOLOGY UPDATE"
        tags = "#EmergingTechnology"
        format_type = "technology"
    elif category == Category.SOFTWARE:
        title = "🧑‍💻 DEVELOPER UPDATE"
        tags = "#Software #OpenSource"
        format_type = "technology"
    else:
        title = "📰 TECHNOLOGY UPDATE"
        tags = "#Technology"
        format_type = "technology"
    return f"{title}\n\n{summary}\n\n{source_line}\n\n{tags}", format_type
