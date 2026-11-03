from app.api.schemas import Category


def build_draft(
    *,
    category: Category,
    headline: str,
    summary: str,
    why_it_matters: str | None,
    source_url: str | None,
    source_name: str,
) -> tuple[str, str]:
    source_line = (
        f"Source: {source_name} | {source_url}"
        if source_url
        else (f"Source: {source_name} (user-provided text; no source URL)")
    )
    if category == Category.CYBERSECURITY:
        body = (
            "🔐 CYBERSECURITY UPDATE\n\n"
            f"{headline}\n\n"
            f"What is known:\n{summary}\n\n"
            f"Sources: {source_line}\n"
            "#Cybersecurity #Security"
        )
        return body, "cybersecurity"
    if category == Category.AI:
        impact_section = f"\n\nWhy it matters:\n{why_it_matters}" if why_it_matters else ""
        body = (
            "🤖 AI UPDATE\n\n"
            f"{headline}\n\n"
            f"What happened:\n{summary}\n\n"
            f"{impact_section.lstrip()}\n\n"
            f"{source_line}\n"
            "#AI #MachineLearning"
        )
        return body, "ai"
    if category == Category.SCIENCE:
        body = (
            "🔬 RESEARCH EXPLAINED\n\n"
            f"{headline}\n\n"
            f"Key finding:\n{summary}\n\n"
            f"Source: {source_line}\n"
            "#Research #Science"
        )
        return body, "research"
    if category == Category.EMERGING_TECH:
        title = "🚀 TECHNOLOGY UPDATE"
        tags = "#EmergingTechnology"
    elif category == Category.SOFTWARE:
        title = "🧑‍💻 DEVELOPER UPDATE"
        tags = "#Software #OpenSource"
    else:
        title = "📰 TECHNOLOGY UPDATE"
        tags = "#Technology"
    return f"{title}\n\n{headline}\n\n{summary}\n\n{source_line}\n{tags}", "technology"
