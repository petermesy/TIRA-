import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.schemas import (
    AnalysisResponse,
    AnalyzeArticleRequest,
    AnalyzeArticleResponse,
    ArticleResponse,
    DraftResponse,
    ScoreBreakdown,
)
from app.core.config import Settings
from app.core.security import require_admin
from app.database.models import (
    Analysis,
    Article,
    Draft,
    VerificationRun,
    VerifiedClaim,
)
from app.database.session import get_db
from app.intelligence.analyzer.service import AnalysisInvalidResponse
from app.services.article_service import (
    DuplicateArticleError,
    analyze_and_persist,
    article_response,
)
from app.sources.manual.url_reader import ArticleExtractionError
from app.verification.schemas import VerificationResponse
from app.verification.service import VerificationService

router = APIRouter(prefix="/api/v1", tags=["articles"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _analysis_response(analysis: Analysis) -> AnalysisResponse:
    return AnalysisResponse(
        id=analysis.id,
        provider=analysis.provider,
        model_name=analysis.model_name,
        prompt_version=analysis.prompt_version,
        generation_settings=analysis.generation_settings,
        scores=ScoreBreakdown(
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


@router.post("/articles/analyze", response_model=AnalyzeArticleResponse, status_code=201)
async def analyze_article(
    payload: AnalyzeArticleRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> AnalyzeArticleResponse:
    settings = _settings(request)
    if payload.text is not None and len(payload.text) > settings.max_article_chars:
        raise HTTPException(
            status_code=413,
            detail=f"Article text exceeds the {settings.max_article_chars}-character limit",
        )
    try:
        article, analysis, draft, recommendation = await analyze_and_persist(
            db,
            settings,
            url=payload.url,
            text=payload.text,
            title=payload.title,
            category_hint=payload.category_hint,
            language=payload.language,
        )
    except DuplicateArticleError as exc:
        existing = exc.article
        current_analysis = max(existing.analyses, key=lambda item: item.created_at, default=None)
        current_draft = max(existing.drafts, key=lambda item: item.version, default=None)
        if current_analysis is None or current_draft is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Duplicate article exists but does not have a complete analysis",
            ) from exc
        recommendation = (
            "REJECT"
            if current_analysis.importance < 4
            else "REVIEW"
            if current_analysis.importance >= settings.min_importance_score
            else "NEEDS_REVIEW"
        )
        article, analysis, draft = existing, current_analysis, current_draft
    except ArticleExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AnalysisInvalidResponse as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return AnalyzeArticleResponse(
        article_id=article.id,
        draft_id=draft.id,
        category=article.category,
        scores=ScoreBreakdown(
            relevance=analysis.relevance,
            novelty=analysis.novelty,
            impact=analysis.impact,
            credibility=analysis.credibility,
            technical_significance=analysis.technical_significance,
            importance=analysis.importance,
        ),
        recommendation=recommendation,
        quality_status=draft.quality_status,
        status=article.status,
        warnings=analysis.warnings + draft.quality_findings,
    )


@router.get("/articles/{article_id}", response_model=ArticleResponse)
def get_article(article_id: str, db: Session = Depends(get_db)) -> ArticleResponse:
    article = db.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return article_response(article)


@router.get("/articles/{article_id}/draft", response_model=DraftResponse)
def get_current_draft(article_id: str, db: Session = Depends(get_db)) -> DraftResponse:
    article = db.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    draft = db.scalar(
        select(Draft).where(Draft.article_id == article_id).order_by(Draft.version.desc()).limit(1)
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return DraftResponse.model_validate(draft)


@router.post("/articles/{article_id}/verify", response_model=VerificationResponse)
async def verify_article(
    article_id: str,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> VerificationResponse:
    article = db.get(Article, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    try:
        settings = _settings(request)
        async with asyncio.timeout(settings.max_verification_duration_seconds):
            verification = await VerificationService(settings, db).verify(article)
    except TimeoutError as exc:
        db.rollback()
        raise HTTPException(
            status_code=504,
            detail="Article verification exceeded its time limit",
        ) from exc
    except AnalysisInvalidResponse as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return VerificationResponse.model_validate(verification)


@router.get(
    "/articles/{article_id}/verification",
    response_model=VerificationResponse | None,
)
def get_article_verification(
    article_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> VerificationResponse | None:
    if db.get(Article, article_id) is None:
        raise HTTPException(status_code=404, detail="Article not found")
    verification = db.scalar(
        select(VerificationRun)
        .options(selectinload(VerificationRun.claims).selectinload(VerifiedClaim.evidence))
        .where(VerificationRun.article_id == article_id)
        .order_by(VerificationRun.version.desc())
        .limit(1)
    )
    return VerificationResponse.model_validate(verification) if verification else None
