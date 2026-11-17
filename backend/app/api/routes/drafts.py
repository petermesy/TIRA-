from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.schemas import (
    DeliveryResponse,
    DraftResponse,
    EditDraftRequest,
    ReviewRequest,
    ReviewResponse,
)
from app.core.config import Settings
from app.core.security import require_admin
from app.database.models import (
    Article,
    AuditEvent,
    Draft,
    PostCorrection,
    PublishedPost,
    ReviewDecision,
    VerificationRun,
    VerifiedClaim,
)
from app.database.session import get_db
from app.intelligence.quality.checks import check_draft
from app.services.operations import get_operations_control, published_today_count
from app.telegram.client import TelegramClient, TelegramDeliveryError

router = APIRouter(prefix="/api/v1/drafts", tags=["drafts"])


class CorrectionRequest(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)
    reason: str = Field(min_length=10, max_length=2_000)


class CorrectionResponse(BaseModel):
    correction_id: str
    original_post_id: str
    correction_draft_id: str
    reason: str
    status: str


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


def _auto_publish_reasons(
    db: Session,
    settings: Settings,
    article: Article,
    draft: Draft,
) -> list[str]:
    reasons: list[str] = []
    if settings.publish_mode != "auto_with_gates" or not settings.auto_publish_enabled:
        reasons.append("Automatic publishing is disabled")
    if article.category not in settings.auto_publish_categories:
        reasons.append("Article category is not enabled for automatic publishing")
    if draft.quality_status != "PASS":
        reasons.append("Draft quality checks did not pass")
    verification = db.scalar(
        select(VerificationRun)
        .options(selectinload(VerificationRun.claims).selectinload(VerifiedClaim.evidence))
        .where(VerificationRun.article_id == article.id)
        .order_by(VerificationRun.version.desc())
        .limit(1)
    )
    if verification is None:
        reasons.append("No claim verification is available")
    else:
        if verification.importance_score < settings.min_auto_importance_score:
            reasons.append("Importance score is below the automatic publishing threshold")
        if verification.status != "CONFIRMED":
            reasons.append("Aggregate claim status is not CONFIRMED")
        if verification.verification_score < settings.min_auto_verification_score:
            reasons.append("Verification score is below the automatic publishing threshold")
        material_claims = [claim for claim in verification.claims if claim.is_material]
        if not material_claims or any(claim.status != "CONFIRMED" for claim in material_claims):
            reasons.append("One or more material claims are unresolved")
        if not any(
            evidence.evidence_type == "PRIMARY"
            and evidence.stance == "SUPPORTS"
            and evidence.source_tier in {"HIGH", "AUTHORITATIVE"}
            for claim in material_claims
            for evidence in claim.evidence
        ):
            reasons.append("No reliable primary evidence supports a material claim")
    return reasons


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
    verification = db.scalar(
        select(VerificationRun)
        .where(VerificationRun.article_id == article.id)
        .order_by(VerificationRun.version.desc())
        .limit(1)
    )
    evidence_urls: list[str] = []
    if verification:
        claims = db.scalars(
            select(VerifiedClaim).where(VerifiedClaim.verification_run_id == verification.id)
        ).all()
        evidence_urls = [item.url for claim in claims for item in claim.evidence[:2]][:5]
    latest_analysis = max(article.analyses, key=lambda item: item.created_at, default=None)
    card_lines = [
        "TIRA REVIEW",
        f"Category: {article.category}",
        (
            f"Importance: {latest_analysis.importance:.2f}/10 | "
            f"Verification: {verification.verification_score:.0%} — {verification.status}"
            if verification
            else (
                f"Importance: {latest_analysis.importance:.2f}/10 | Verification: NOT RUN"
                if latest_analysis
                else "Verification: NOT RUN"
            )
        ),
        f"Headline: {article.title}",
        f"Draft:\n{draft.body}",
    ]
    if evidence_urls:
        card_lines.append("Evidence:\n" + "\n".join(evidence_urls))
    elif verification:
        card_lines.append("Unresolved: No evidence retrieved; claims remain unconfirmed.")
    if verification and verification.warnings:
        card_lines.append("Review notes: " + "; ".join(verification.warnings[:3]))
    review_card = "\n\n".join(card_lines)[: settings.max_telegram_message_length]
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "Approve and publish", "callback_data": f"approve:{draft.id}"},
                {"text": "Reject", "callback_data": f"reject:{draft.id}"},
            ]
        ]
    }
    try:
        message_id = await TelegramClient(settings).send_message(
            settings.telegram_review_chat_id,
            review_card,
            reply_markup=reply_markup,
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
    correction = db.scalar(
        select(PostCorrection).where(PostCorrection.correction_draft_id == draft.id)
    )
    if article.status == "PUBLISHED" and correction is None:
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
    if correction:
        correction.correction_draft_id = new_draft.id
    else:
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
    correction = db.scalar(
        select(PostCorrection).where(PostCorrection.correction_draft_id == draft.id)
    )
    if article.status == "PUBLISHED" and correction is None:
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
    if correction is None:
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
    correction = db.scalar(
        select(PostCorrection).where(PostCorrection.correction_draft_id == draft.id)
    )
    if article.status == "PUBLISHED" and correction is None:
        raise HTTPException(status_code=409, detail="Published article cannot be rejected")
    decision = ReviewDecision(
        draft_id=draft.id,
        reviewer_id=reviewer_id,
        decision="REJECTED",
        comment=payload.comment,
    )
    if correction is None:
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


@router.post("/published/{post_id}/correction", response_model=CorrectionResponse)
def create_correction(
    post_id: str,
    payload: CorrectionRequest,
    request: Request,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> CorrectionResponse:
    original = db.get(PublishedPost, post_id)
    if original is None or original.delivery_status != "DELIVERED":
        raise HTTPException(status_code=404, detail="Published post not found")
    original_draft = db.get(Draft, original.draft_id)
    article = db.get(Article, original_draft.article_id) if original_draft else None
    if original_draft is None or article is None:
        raise HTTPException(status_code=409, detail="Published post has no source article")
    existing = db.scalar(
        select(PostCorrection).where(
            PostCorrection.original_post_id == original.id,
            PostCorrection.reason == payload.reason.strip(),
        )
    )
    if existing is not None:
        return CorrectionResponse(
            correction_id=existing.id,
            original_post_id=original.id,
            correction_draft_id=existing.correction_draft_id,
            reason=existing.reason,
            status="PENDING_APPROVAL",
        )
    source_reference = article.canonical_url or article.source_name
    body = f"CORRECTION\n\n{payload.body.strip()}\n\nSource: {source_reference}"
    findings = check_draft(
        body=body,
        source_url=article.canonical_url,
        source_name=article.source_name,
        source_text=f"{article.title} {article.content_excerpt}",
        settings=_settings(request),
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
    draft = Draft(
        article_id=article.id,
        body=body,
        format_type="correction",
        version=current_version + 1,
        quality_status="PASS" if not findings else "NEEDS_REVIEW",
        quality_findings=findings,
    )
    db.add(draft)
    db.flush()
    correction = PostCorrection(
        original_post_id=original.id,
        correction_draft_id=draft.id,
        reason=payload.reason.strip(),
        created_by=reviewer_id,
    )
    db.add(correction)
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=draft.id,
            operation="correction_created",
            status="PENDING_APPROVAL",
            actor=reviewer_id,
            details={
                "original_post_id": original.id,
                "reason": correction.reason,
                "findings": findings,
            },
        )
    )
    db.commit()
    db.refresh(correction)
    return CorrectionResponse(
        correction_id=correction.id,
        original_post_id=original.id,
        correction_draft_id=draft.id,
        reason=correction.reason,
        status="PENDING_APPROVAL",
    )


@router.post("/{draft_id}/publish", response_model=DeliveryResponse)
async def publish_draft(
    draft_id: str,
    request: Request,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> DeliveryResponse:
    publish_lock = request.app.state.publish_lock
    if publish_lock.locked():
        raise HTTPException(status_code=409, detail="A publication is already in progress")
    async with publish_lock:
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
        correction = db.scalar(
            select(PostCorrection).where(PostCorrection.correction_draft_id == draft.id)
        )
        is_correction = correction is not None
        control = get_operations_control(db)
        if control.publishing_paused:
            raise HTTPException(
                status_code=423,
                detail="Publishing is paused by an administrator",
            )
        if not is_correction and published_today_count(db) >= settings.max_posts_per_day:
            raise HTTPException(
                status_code=429,
                detail="The configured daily post limit was reached",
            )
        decision = _latest_decision(db, draft.id)
        automatic = False
        if decision is not None and decision.decision == "REJECTED":
            raise HTTPException(status_code=409, detail="Rejected drafts cannot be published")
        if decision is None:
            reasons = _auto_publish_reasons(db, settings, article, draft)
            if reasons:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Draft requires explicit approval; automatic gates did not pass",
                        "reasons": reasons,
                    },
                )
            decision = ReviewDecision(
                draft_id=draft.id,
                reviewer_id="system:auto",
                decision="AUTO_APPROVED",
                comment="All configured V3 automatic publishing gates passed.",
            )
            db.add(decision)
            automatic = True
        if (
            (
                article.status not in {"APPROVED", "PUBLISH_FAILED", "PENDING_APPROVAL"}
                and not (is_correction and article.status == "PUBLISHED")
            )
            or decision is None
            or decision.decision not in {"APPROVED", "AUTO_APPROVED"}
        ):
            raise HTTPException(status_code=409, detail="Draft is not eligible for publication")
        if draft.quality_status != "PASS":
            raise HTTPException(
                status_code=422, detail="Draft quality checks must pass before publication"
            )
        if delivery and delivery.delivery_status == "SENDING":
            raise HTTPException(
                status_code=409, detail="A publication attempt is already in progress"
            )
        if delivery and delivery.delivery_status == "FAILED":
            if not delivery.retryable or delivery.attempt_count >= settings.telegram_max_attempts:
                raise HTTPException(
                    status_code=409,
                    detail="This Telegram failure is permanent or exhausted its retry limit",
                )
            if delivery.next_retry_at:
                next_retry = (
                    delivery.next_retry_at.replace(tzinfo=UTC)
                    if delivery.next_retry_at.tzinfo is None
                    else delivery.next_retry_at
                )
                wait_seconds = int((next_retry - datetime.now(UTC)).total_seconds())
                if wait_seconds > 0:
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "message": "Telegram retry backoff is active",
                            "retry_after_seconds": wait_seconds,
                        },
                        headers={"Retry-After": str(wait_seconds)},
                    )

        if delivery is None:
            delivery = PublishedPost(
                draft_id=draft.id,
                channel_id=settings.telegram_channel_id,
                delivery_status="SENDING",
                attempt_count=1,
                response_metadata={"decision": "AUTO_APPROVED" if automatic else "APPROVED"},
            )
            db.add(delivery)
        else:
            delivery.delivery_status = "SENDING"
            delivery.error_class = None
            delivery.retryable = False
            delivery.next_retry_at = None
            delivery.attempt_count += 1
            delivery.channel_id = settings.telegram_channel_id
            delivery.response_metadata = {
                **delivery.response_metadata,
                "decision": "AUTO_APPROVED" if automatic else "APPROVED",
            }
        if not is_correction:
            article.status = "APPROVED"
        db.commit()
        db.refresh(delivery)

        try:
            message_id = await TelegramClient(settings).send_message(
                settings.telegram_channel_id,
                draft.body,
            )
        except TelegramDeliveryError as exc:
            delivery.delivery_status = "FAILED"
            delivery.error_class = exc.error_class
            delivery.retryable = (
                exc.retryable and delivery.attempt_count < settings.telegram_max_attempts
            )
            retry_after = min(
                settings.telegram_retry_base_seconds * (2 ** max(0, delivery.attempt_count - 1)),
                settings.telegram_retry_max_seconds,
            )
            delivery.next_retry_at = (
                datetime.now(UTC) + timedelta(seconds=retry_after) if delivery.retryable else None
            )
            delivery.response_metadata = {
                **delivery.response_metadata,
                "error_class": exc.error_class,
                "retryable": delivery.retryable,
                "attempt_count": delivery.attempt_count,
            }
            if not is_correction:
                article.status = "PUBLISH_FAILED"
            db.add(
                AuditEvent(
                    article_id=article.id,
                    draft_id=draft.id,
                    operation="telegram_publish",
                    status="FAILED",
                    actor="system:auto" if automatic else reviewer_id,
                    details={
                        "error_class": exc.error_class,
                        "retryable": delivery.retryable,
                        "attempt_count": delivery.attempt_count,
                    },
                )
            )
            db.commit()
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        delivery.delivery_status = "DELIVERED"
        delivery.message_id = message_id
        delivery.response_metadata = {**delivery.response_metadata, "message_id": message_id}
        delivery.published_at = datetime.now(UTC)
        article.status = "PUBLISHED"
        db.add(
            AuditEvent(
                article_id=article.id,
                draft_id=draft.id,
                operation="telegram_publish",
                status="DELIVERED",
                actor="system:auto" if automatic else reviewer_id,
                details={
                    "channel_id": settings.telegram_channel_id,
                    "message_id": message_id,
                    "decision": "AUTO_APPROVED" if automatic else "APPROVED",
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
