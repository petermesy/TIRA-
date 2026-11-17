from datetime import UTC, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.operations_schemas import (
    AutoPublishOutcome,
    AutoPublishResponse,
    DailySummaryResponse,
    OperationsControlResponse,
    PauseRequest,
    ReviewQueueResponse,
)
from app.api.routes.drafts import publish_draft
from app.core.config import Settings
from app.core.security import require_admin
from app.database.models import (
    Article,
    AuditEvent,
    CollectionRun,
    Draft,
    PublishedPost,
    ReviewDecision,
)
from app.database.session import get_db
from app.services.article_service import article_response
from app.services.operations import get_operations_control, published_today_count

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/status", response_model=OperationsControlResponse)
def operations_status(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> OperationsControlResponse:
    control = get_operations_control(db)
    latest_run = db.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1))
    pending_count = int(
        db.scalar(select(func.count(Article.id)).where(Article.status == "PENDING_APPROVAL")) or 0
    )
    return OperationsControlResponse(
        publishing_paused=control.publishing_paused,
        collection_paused=control.collection_paused,
        publish_mode=_settings(request).publish_mode,
        auto_publish_enabled=_settings(request).auto_publish_enabled,
        max_posts_per_day=_settings(request).max_posts_per_day,
        posts_published_today=published_today_count(db),
        pending_review_count=pending_count,
        latest_collection_status=latest_run.status if latest_run else None,
        latest_errors=latest_run.errors[:10] if latest_run else [],
        reason=control.reason,
        updated_by=control.updated_by,
        updated_at=control.updated_at,
    )


@router.post("/pause", response_model=OperationsControlResponse)
def pause_operations(
    payload: PauseRequest,
    request: Request,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> OperationsControlResponse:
    control = get_operations_control(db)
    if payload.scope in {"publishing", "all"}:
        control.publishing_paused = True
    if payload.scope in {"collection", "all"}:
        control.collection_paused = True
    control.reason = payload.reason.strip()
    control.updated_by = reviewer_id
    db.add(
        AuditEvent(
            operation="operations_paused",
            status="PAUSED",
            actor=reviewer_id,
            details={"scope": payload.scope, "reason": control.reason},
        )
    )
    db.commit()
    return operations_status(request, db, reviewer_id)


@router.post("/resume", response_model=OperationsControlResponse)
def resume_operations(
    payload: PauseRequest,
    request: Request,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> OperationsControlResponse:
    control = get_operations_control(db)
    if payload.scope in {"publishing", "all"}:
        control.publishing_paused = False
    if payload.scope in {"collection", "all"}:
        control.collection_paused = False
    if not control.publishing_paused and not control.collection_paused:
        control.reason = None
    control.updated_by = reviewer_id
    db.add(
        AuditEvent(
            operation="operations_resumed",
            status="RESUMED",
            actor=reviewer_id,
            details={"scope": payload.scope, "reason": payload.reason.strip()},
        )
    )
    db.commit()
    return operations_status(request, db, reviewer_id)


@router.get("/review-queue", response_model=ReviewQueueResponse)
def review_queue(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> ReviewQueueResponse:
    articles = db.scalars(
        select(Article)
        .where(Article.status == "PENDING_APPROVAL")
        .order_by(Article.discovered_at.desc())
        .limit(100)
    ).all()
    if articles:
        articles.sort(
            key=lambda article: (
                max(article.analyses, key=lambda analysis: analysis.created_at).importance
                if article.analyses
                else 0.0,
                article.discovered_at,
            ),
            reverse=True,
        )
    return ReviewQueueResponse(articles=[article_response(article) for article in articles])


@router.get("/daily-summary", response_model=DailySummaryResponse)
def daily_summary(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> DailySummaryResponse:
    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    collection_runs = (
        db.scalar(
            select(func.count(CollectionRun.id)).where(
                CollectionRun.started_at >= start,
                CollectionRun.started_at < end,
            )
        )
        or 0
    )
    articles = (
        db.scalar(
            select(func.count(Article.id)).where(
                Article.discovered_at >= start,
                Article.discovered_at < end,
            )
        )
        or 0
    )
    approved = (
        db.scalar(
            select(func.count(ReviewDecision.id)).where(
                ReviewDecision.decided_at >= start,
                ReviewDecision.decided_at < end,
                ReviewDecision.decision.in_(("APPROVED", "AUTO_APPROVED")),
            )
        )
        or 0
    )
    rejected = (
        db.scalar(
            select(func.count(ReviewDecision.id)).where(
                ReviewDecision.decided_at >= start,
                ReviewDecision.decided_at < end,
                ReviewDecision.decision == "REJECTED",
            )
        )
        or 0
    )
    published = (
        db.scalar(
            select(func.count(PublishedPost.id)).where(
                PublishedPost.delivery_status == "DELIVERED",
                PublishedPost.published_at >= start,
                PublishedPost.published_at < end,
            )
        )
        or 0
    )
    failed = (
        db.scalar(
            select(func.count(PublishedPost.id)).where(
                PublishedPost.delivery_status == "FAILED",
                PublishedPost.updated_at >= start,
                PublishedPost.updated_at < end,
            )
        )
        or 0
    )
    pending = (
        db.scalar(select(func.count(Article.id)).where(Article.status == "PENDING_APPROVAL")) or 0
    )
    return DailySummaryResponse(
        day=start.date(),
        collection_runs=collection_runs,
        articles_discovered=articles,
        approved=approved,
        rejected=rejected,
        published=published,
        failed_deliveries=failed,
        pending_review=pending,
    )


@router.post("/auto-publish", response_model=AutoPublishResponse)
async def auto_publish_eligible(
    request: Request,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> AutoPublishResponse:
    settings = _settings(request)
    if settings.publish_mode != "auto_with_gates" or not settings.auto_publish_enabled:
        raise HTTPException(
            status_code=409,
            detail="Automatic publishing is not explicitly enabled",
        )
    control = get_operations_control(db)
    if control.publishing_paused:
        raise HTTPException(status_code=423, detail="Publishing is paused by an administrator")
    remaining = max(0, settings.max_posts_per_day - published_today_count(db))
    if remaining == 0:
        return AutoPublishResponse(attempted=0, published=0, outcomes=[])
    articles = db.scalars(
        select(Article)
        .where(Article.status == "PENDING_APPROVAL")
        .order_by(Article.discovered_at.asc())
        .limit(100)
    ).all()
    candidates: list[tuple[float, str]] = []
    for article in articles:
        latest_draft = db.scalar(
            select(Draft)
            .where(Draft.article_id == article.id)
            .order_by(Draft.version.desc())
            .limit(1)
        )
        if latest_draft is None or latest_draft.quality_status != "PASS":
            continue
        latest_analysis = max(
            article.analyses,
            key=lambda analysis: analysis.created_at,
            default=None,
        )
        candidates.append(
            (
                latest_analysis.importance if latest_analysis else 0.0,
                latest_draft.id,
            )
        )
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)

    outcomes: list[AutoPublishOutcome] = []
    published = 0
    for _, draft_id in candidates[:remaining]:
        try:
            result = await publish_draft(draft_id, request, db, "system:auto")
        except HTTPException as exc:
            outcomes.append(
                AutoPublishOutcome(
                    draft_id=draft_id,
                    status="SKIPPED",
                    detail=str(exc.detail),
                )
            )
            if exc.status_code == 429:
                break
        else:
            published += 1
            outcomes.append(AutoPublishOutcome(draft_id=draft_id, status=result.delivery_status))
    return AutoPublishResponse(
        attempted=len(outcomes),
        published=published,
        outcomes=outcomes,
    )
