from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.schemas import (
    AnalysisResponse,
    ArticleResponse,
    Category,
    DraftResponse,
)
from app.api.schemas import (
    ScoreBreakdown as ApiScoreBreakdown,
)
from app.core.config import Settings
from app.core.security import canonicalize_url
from app.database.models import Analysis, Article, AuditEvent, Draft, Source
from app.intelligence.analyzer.service import (
    PROMPT_VERSION,
    AnalyzerService,
)
from app.intelligence.normalizer import content_hash, normalize_text, source_domain
from app.intelligence.quality.checks import check_draft
from app.intelligence.scoring.calculator import build_score_breakdown
from app.intelligence.writer.templates import build_draft
from app.sources.manual.text_reader import read_manual_text
from app.sources.manual.url_reader import extract_url


class DuplicateArticleError(Exception):
    def __init__(self, article: Article) -> None:
        super().__init__("An article with the same canonical URL or content already exists")
        self.article = article


async def analyze_and_persist(
    db: Session,
    settings: Settings,
    *,
    url: str | None,
    text: str | None,
    title: str | None,
    category_hint: Category | None,
    language: str,
    source_name: str | None = None,
    published_at: datetime | None = None,
    author: str | None = None,
    image_url: str | None = None,
    retrieval_method: str = "MANUAL",
    registered_source_id: str | None = None,
) -> tuple[Article, Analysis, Draft, str]:
    resolved_image_url = image_url
    if url and text is None:
        extracted = await extract_url(url, settings)
        canonical_url = extracted.url
        resolved_title = (title or extracted.title).strip()
        content = normalize_text(extracted.text, settings.max_article_chars)
        resolved_image_url = extracted.image_url or resolved_image_url
        resolved_source_name = source_name or extracted.source_name
        source_domain_value = source_domain(canonical_url)
    else:
        resolved_title, content = read_manual_text(text or "", title, settings)
        canonical_url = canonicalize_url(url) if url else None
        resolved_source_name = source_name or (
            source_domain(canonical_url) if canonical_url else "User-provided text"
        )
        source_domain_value = source_domain(canonical_url)

    if not resolved_title:
        raise ValueError("Article title must not be empty")
    digest = content_hash(content)
    duplicate_query = select(Article).where(
        or_(Article.content_hash == digest, Article.canonical_url == canonical_url)
        if canonical_url
        else Article.content_hash == digest
    )
    existing = db.scalar(duplicate_query)
    if existing is not None:
        raise DuplicateArticleError(existing)

    source_type = (
        "MANUAL_URL"
        if retrieval_method == "MANUAL" and canonical_url
        else "MANUAL_TEXT"
        if retrieval_method == "MANUAL"
        else retrieval_method
    )
    source = db.get(Source, registered_source_id) if registered_source_id else None
    if source is None:
        source = db.scalar(
            select(Source).where(
                Source.domain == source_domain_value
                if source_domain_value
                else (Source.name == resolved_source_name) & (Source.source_type == source_type)
            )
        )
    if source is None:
        source = Source(
            name=resolved_source_name,
            source_type=source_type,
            base_url=(
                f"{urlsplit(canonical_url).scheme}://{urlsplit(canonical_url).netloc}"
                if canonical_url
                else None
            ),
            domain=source_domain_value,
        )
        db.add(source)
        db.flush()

    analysis_result = await AnalyzerService(settings).analyze(
        title=resolved_title,
        text=content,
        url=canonical_url,
        category_hint=category_hint,
    )
    output = analysis_result.output
    dimensions = output.scores.model_dump()
    score = build_score_breakdown(dimensions)
    body, format_type = build_draft(
        category=output.category,
        summary=output.summary,
        source_url=canonical_url,
        source_name=resolved_source_name,
    )
    quality_findings = check_draft(
        body=body,
        source_url=canonical_url,
        source_name=resolved_source_name,
        source_text=f"{resolved_title} {content}",
        settings=settings,
    )
    article_status = "PENDING_APPROVAL"
    article = Article(
        canonical_url=canonical_url,
        source_id=source.id,
        title=resolved_title[:500],
        source_name=resolved_source_name[:255],
        source_domain=source_domain_value,
        author=author,
        published_at=published_at,
        image_url=resolved_image_url,
        discovered_at=datetime.now(UTC),
        category=output.category.value,
        language=language.lower(),
        content_excerpt=content[: settings.max_article_chars],
        content_hash=digest,
        retrieval_method=retrieval_method,
        status=article_status,
    )
    analysis = Analysis(
        provider=analysis_result.provider,
        model_name=analysis_result.model_name,
        prompt_version=PROMPT_VERSION,
        generation_settings=analysis_result.generation_settings,
        relevance=score.relevance,
        novelty=score.novelty,
        impact=score.impact,
        credibility=score.credibility,
        technical_significance=score.technical_significance,
        importance=score.importance,
        rationale=output.rationale,
        evidence_references=output.evidence_references,
        summary=output.summary,
        why_it_matters=output.why_it_matters,
        headline=output.headline,
        warnings=analysis_result.warnings,
    )
    draft = Draft(
        body=body,
        format_type=format_type,
        version=1,
        quality_status="PASS" if not quality_findings else "NEEDS_REVIEW",
        quality_findings=quality_findings,
    )
    article.analyses.append(analysis)
    article.drafts.append(draft)
    db.add(article)
    db.flush()
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=draft.id,
            operation="article_analyzed",
            status=article_status,
            details={
                "model_name": analysis_result.model_name,
                "importance": score.importance,
                "quality_status": draft.quality_status,
                "warnings": analysis_result.warnings + quality_findings,
            },
        )
    )
    db.commit()
    db.refresh(article)
    db.refresh(analysis)
    db.refresh(draft)
    recommendation = (
        "REJECT"
        if score.importance < 4
        else "REVIEW"
        if score.importance >= settings.min_importance_score
        else "NEEDS_REVIEW"
    )
    return article, analysis, draft, recommendation


def article_response(article: Article) -> ArticleResponse:
    analysis = max(article.analyses, key=lambda item: item.created_at, default=None)
    draft = max(article.drafts, key=lambda item: item.version, default=None)
    analysis_response = None
    if analysis:
        analysis_response = AnalysisResponse(
            id=analysis.id,
            provider=analysis.provider,
            model_name=analysis.model_name,
            prompt_version=analysis.prompt_version,
            generation_settings=analysis.generation_settings,
            scores=ApiScoreBreakdown(
                relevance=analysis.relevance,
                novelty=analysis.novelty,
                impact=analysis.impact,
                credibility=analysis.credibility,
                technical_significance=analysis.technical_significance,
                importance=analysis.importance,
            ),
            rationale=analysis.rationale,
            evidence_references=analysis.evidence_references,
            summary=analysis.summary,
            why_it_matters=analysis.why_it_matters,
            headline=analysis.headline,
            warnings=analysis.warnings,
            created_at=analysis.created_at,
        )
    draft_response = DraftResponse.model_validate(draft) if draft else None
    return ArticleResponse(
        id=article.id,
        canonical_url=article.canonical_url,
        title=article.title,
        source_name=article.source_name,
        source_domain=article.source_domain,
        author=article.author,
        published_at=article.published_at,
        image_url=article.image_url,
        discovered_at=article.discovered_at,
        category=Category(article.category),
        language=article.language,
        content_excerpt=article.content_excerpt,
        content_hash=article.content_hash,
        retrieval_method=article.retrieval_method,
        status=article.status,
        analysis=analysis_response,
        current_draft=draft_response,
    )


def score_breakdown(analysis: Analysis) -> ApiScoreBreakdown:
    return ApiScoreBreakdown(
        relevance=analysis.relevance,
        novelty=analysis.novelty,
        impact=analysis.impact,
        credibility=analysis.credibility,
        technical_significance=analysis.technical_significance,
        importance=analysis.importance,
    )
