from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    DeliveryResponse,
    DraftResponse,
    EditDraftRequest,
    ReviewRequest,
    ReviewResponse,
)
from app.core.config import Settings
from app.core.security import require_admin
from app.database.models import Article, AuditEvent, Draft, PublishedPost, ReviewDecision
from app.database.session import get_db
from app.intelligence.quality.checks import check_draft
from app.telegram.client import TelegramClient, TelegramDeliveryError

router = APIRouter(prefix="/api/v1/drafts", tags=["drafts"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _load_draft(db: Session, draft_id: str) -> tuple[Draft, Article]:
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    article = db.get(Article, draft.article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    return draft, article


def _latest_decision(db: Session, draft_id: str) -> ReviewDecision | None:
    return db.scalar(
        select(ReviewDecision)
        .where(ReviewDecision.draft_id == draft_id)
        .order_by(ReviewDecision.decided_at.desc())
        .limit(1)
    )


@router.post("/{draft_id}/preview", response_model=DeliveryResponse)
async def preview_draft(
    draft_id: str,
    request: Request,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> DeliveryResponse:
    draft, article = _load_draft(db, draft_id)
    settings = _settings(request)
    if not settings.telegram_review_chat_id:
        raise HTTPException(status_code=503, detail="TELEGRAM_REVIEW_CHAT_ID is not configured")
    try:
        message_id = await TelegramClient(settings).send_message(
            settings.telegram_review_chat_id, draft.body
        )
    except TelegramDeliveryError as exc:
        db.add(
            AuditEvent(
                article_id=article.id,
                draft_id=draft.id,
                operation="telegram_preview",
                status="FAILED",
                actor=reviewer_id,
                details={"error_class": exc.error_class},
            )
        )
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=draft.id,
            operation="telegram_preview",
            status="DELIVERED",
            actor=reviewer_id,
            details={"chat_id": settings.telegram_review_chat_id, "message_id": message_id},
        )
    )
    db.commit()
    return DeliveryResponse(
        draft_id=draft.id,
        delivery_status="DELIVERED",
        channel_id=settings.telegram_review_chat_id,
        message_id=message_id,
    )


@router.post("/{draft_id}/edit", response_model=DraftResponse)
def edit_draft(
    draft_id: str,
    payload: EditDraftRequest,
    request: Request,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> DraftResponse:
    draft, article = _load_draft(db, draft_id)
    if article.status == "PUBLISHED":
        raise HTTPException(
            status_code=409, detail="Published drafts require the correction workflow"
        )
    settings = _settings(request)
    body = payload.body.strip()
    findings = check_draft(
        body=body,
        source_url=article.canonical_url,
        source_name=article.source_name,
        source_text=f"{article.title} {article.content_excerpt}",
        settings=settings,
    )
    current_version = (
        db.scalar(
            select(Draft.version)
            .where(Draft.article_id == article.id)
            .order_by(Draft.version.desc())
            .limit(1)
        )
        or 0
    )
    new_draft = Draft(
        article_id=article.id,
        body=body,
        format_type=draft.format_type,
        version=current_version + 1,
        quality_status="PASS" if not findings else "NEEDS_REVIEW",
        quality_findings=findings,
    )
    article.status = "PENDING_APPROVAL"
    db.add(new_draft)
    db.flush()
    db.add(
        ReviewDecision(
            draft_id=new_draft.id,
            reviewer_id=reviewer_id,
            decision="EDITED",
            comment=payload.comment,
        )
    )
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=new_draft.id,
            operation="draft_edited",
            status=new_draft.quality_status,
            actor=reviewer_id,
            details={"version": new_draft.version, "findings": findings},
        )
    )
    db.commit()
    db.refresh(new_draft)
    return DraftResponse.model_validate(new_draft)


@router.post("/{draft_id}/approve", response_model=ReviewResponse)
def approve_draft(
    draft_id: str,
    payload: ReviewRequest,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> ReviewResponse:
    draft, article = _load_draft(db, draft_id)
    if article.status == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Published article cannot be approved again")
    if draft.quality_status != "PASS":
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Resolve draft quality findings before approval",
                "findings": draft.quality_findings,
            },
        )
    latest = _latest_decision(db, draft.id)
    if latest and latest.decision == "APPROVED":
        return ReviewResponse(
            draft_id=draft.id,
            decision="APPROVED",
            reviewer_id=latest.reviewer_id,
            status=article.status,
        )
    decision = ReviewDecision(
        draft_id=draft.id,
        reviewer_id=reviewer_id,
        decision="APPROVED",
        comment=payload.comment,
    )
    article.status = "APPROVED"
    db.add(decision)
    db.flush()
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=draft.id,
            operation="draft_approved",
            status="APPROVED",
            actor=reviewer_id,
            details={"comment": payload.comment},
        )
    )
    db.commit()
    return ReviewResponse(
        draft_id=draft.id, decision="APPROVED", reviewer_id=reviewer_id, status=article.status
    )


@router.post("/{draft_id}/reject", response_model=ReviewResponse)
def reject_draft(
    draft_id: str,
    payload: ReviewRequest,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> ReviewResponse:
    draft, article = _load_draft(db, draft_id)
    if article.status == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Published article cannot be rejected")
    decision = ReviewDecision(
        draft_id=draft.id,
        reviewer_id=reviewer_id,
        decision="REJECTED",
        comment=payload.comment,
    )
    article.status = "REJECTED"
    db.add(decision)
    db.flush()
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=draft.id,
            operation="draft_rejected",
            status="REJECTED",
            actor=reviewer_id,
            details={"comment": payload.comment},
        )
    )
    db.commit()
    return ReviewResponse(
        draft_id=draft.id, decision="REJECTED", reviewer_id=reviewer_id, status=article.status
    )


@router.post("/{draft_id}/publish", response_model=DeliveryResponse)
async def publish_draft(
    draft_id: str,
    request: Request,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> DeliveryResponse:
    draft, article = _load_draft(db, draft_id)
    settings = _settings(request)
    if not settings.telegram_channel_id:
        raise HTTPException(status_code=503, detail="TELEGRAM_CHANNEL_ID is not configured")
    delivery = db.scalar(select(PublishedPost).where(PublishedPost.draft_id == draft.id))
    if delivery and delivery.delivery_status == "DELIVERED":
        return DeliveryResponse(
            draft_id=draft.id,
            delivery_status=delivery.delivery_status,
            channel_id=delivery.channel_id,
            message_id=delivery.message_id,
        )
    decision = _latest_decision(db, draft.id)
    if (
        article.status not in {"APPROVED", "PUBLISH_FAILED"}
        or decision is None
        or decision.decision != "APPROVED"
    ):
        raise HTTPException(
            status_code=409, detail="Draft requires an explicit approval before publication"
        )
    if draft.quality_status != "PASS":
        raise HTTPException(
            status_code=422, detail="Draft quality checks must pass before publication"
        )

    if delivery and delivery.delivery_status == "SENDING":
        raise HTTPException(status_code=409, detail="A publication attempt is already in progress")

    if delivery is None:
        delivery = PublishedPost(
            draft_id=draft.id,
            channel_id=settings.telegram_channel_id,
            delivery_status="SENDING",
        )
        db.add(delivery)
    else:
        delivery.delivery_status = "SENDING"
        delivery.error_class = None
        delivery.channel_id = settings.telegram_channel_id
    article.status = "APPROVED"
    db.commit()
    db.refresh(delivery)

    try:
        message_id = await TelegramClient(settings).send_message(
            settings.telegram_channel_id, draft.body
        )
    except TelegramDeliveryError as exc:
        delivery.delivery_status = "FAILED"
        delivery.error_class = exc.error_class
        article.status = "PUBLISH_FAILED"
        db.add(
            AuditEvent(
                article_id=article.id,
                draft_id=draft.id,
                operation="telegram_publish",
                status="FAILED",
                actor=reviewer_id,
                details={"error_class": exc.error_class},
            )
        )
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    delivery.delivery_status = "DELIVERED"
    delivery.message_id = message_id
    delivery.published_at = datetime.now(UTC)
    article.status = "PUBLISHED"
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=draft.id,
            operation="telegram_publish",
            status="DELIVERED",
            actor=reviewer_id,
            details={
                "channel_id": settings.telegram_channel_id,
                "message_id": message_id,
                "decision": "APPROVED",
            },
        )
    )
    db.commit()
    return DeliveryResponse(
        draft_id=draft.id,
        delivery_status=delivery.delivery_status,
        channel_id=delivery.channel_id,
        message_id=delivery.message_id,
    )
