import asyncio
import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.collection_schemas import SourceCreateRequest
from app.api.routes.collection import register_source
from app.api.schemas import EditDraftRequest, ReviewRequest
from app.collection.scheduler import (
    discover_web_articles,
    discover_web_query,
    send_article_for_review,
)
from app.collection.service import execute_collection
from app.core.config import Settings
from app.database.models import (
    Article,
    AuditEvent,
    CollectionRun,
    Draft,
    PostCorrection,
    ReviewDecision,
    Source,
    TelegramCallback,
)
from app.database.session import get_db
from app.intelligence.analyzer.service import AnalysisInvalidResponse, AnalysisUnavailable
from app.services.article_service import DuplicateArticleError, analyze_and_persist
from app.services.operations import get_operations_control, published_today_count
from app.sources.manual.url_reader import ArticleExtractionError
from app.telegram.client import TelegramClient, TelegramDeliveryError
from app.verification.service import VerificationService

router = APIRouter(prefix="/api/v1/telegram", tags=["telegram"])


class TelegramCallbackActor(BaseModel):
    id: int

    model_config = ConfigDict(extra="ignore")


class TelegramCallbackMessage(BaseModel):
    chat: dict[str, Any]

    model_config = ConfigDict(extra="ignore")


class TelegramCallbackQuery(BaseModel):
    id: str
    data: str = Field(max_length=64)
    from_user: TelegramCallbackActor = Field(alias="from")
    message: TelegramCallbackMessage

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class TelegramMessage(BaseModel):
    chat: dict[str, Any]
    from_user: TelegramCallbackActor = Field(alias="from")
    text: str | None = None

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class TelegramUpdate(BaseModel):
    callback_query: TelegramCallbackQuery | None = None
    message: TelegramMessage | None = None

    model_config = ConfigDict(extra="ignore")


class TelegramWebhookResponse(BaseModel):
    ok: bool
    duplicate: bool = False


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("/webhook", response_model=TelegramWebhookResponse)
async def receive_telegram_update(
    payload: TelegramUpdate,
    request: Request,
    db: Session = Depends(get_db),
    secret_token: str | None = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
) -> TelegramWebhookResponse:
    settings = _settings(request)
    if (
        settings.telegram_webhook_secret is None
        or not settings.telegram_webhook_secret.get_secret_value().strip()
        or not settings.telegram_reviewer_ids
    ):
        raise HTTPException(status_code=503, detail="Telegram review callbacks are not configured")
    expected_secret = settings.telegram_webhook_secret.get_secret_value()
    if secret_token is None or not hmac.compare_digest(secret_token, expected_secret):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")
    callback = payload.callback_query
    if callback is None:
        if payload.message and payload.message.text:
            message = payload.message
            reviewer_id = str(message.from_user.id)
            if reviewer_id not in settings.telegram_reviewer_ids:
                raise HTTPException(
                    status_code=403,
                    detail="Telegram user is not an allowed reviewer",
                )
            chat_id = str(message.chat.get("id", ""))
            is_private_chat = message.chat.get("type") == "private" and chat_id == reviewer_id
            if chat_id != settings.telegram_review_chat_id and not is_private_chat:
                raise HTTPException(
                    status_code=403,
                    detail="Commands are only available in the private review chat or reviewer DM",
                )
            await _handle_command(message.text, chat_id, reviewer_id, settings, db, request)
        return TelegramWebhookResponse(ok=True)
    reviewer_id = str(callback.from_user.id)
    if reviewer_id not in settings.telegram_reviewer_ids:
        raise HTTPException(status_code=403, detail="Telegram user is not an allowed reviewer")
    chat_id = str(callback.message.chat.get("id", ""))
    if not settings.telegram_review_chat_id or chat_id != settings.telegram_review_chat_id:
        raise HTTPException(status_code=403, detail="Callback did not originate in the review chat")

    existing = db.get(TelegramCallback, callback.id)
    if existing is not None:
        await _answer_callback(settings, callback.id, "This action was already processed")
        return TelegramWebhookResponse(ok=True, duplicate=True)

    try:
        action, draft_id = callback.data.split(":", maxsplit=1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid callback action") from exc
    if action not in {"approve", "reject"} or not draft_id:
        raise HTTPException(status_code=422, detail="Unsupported callback action")
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    article = db.get(Article, draft.article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="Article not found")
    correction = db.scalar(
        select(PostCorrection).where(PostCorrection.correction_draft_id == draft.id)
    )
    if article.status == "PUBLISHED" and correction is None:
        raise HTTPException(status_code=409, detail="Published article cannot be reviewed again")
    latest = db.scalar(
        select(ReviewDecision)
        .where(ReviewDecision.draft_id == draft.id)
        .order_by(ReviewDecision.decided_at.desc())
        .limit(1)
    )
    if latest and latest.decision in {"APPROVED", "REJECTED"}:
        raise HTTPException(status_code=409, detail="This draft has already been reviewed")

    if action == "approve":
        if draft.quality_status != "PASS":
            raise HTTPException(status_code=422, detail="Draft quality checks must pass")
        if not latest or latest.decision != "APPROVED":
            db.add(
                ReviewDecision(
                    draft_id=draft.id,
                    reviewer_id=f"telegram:{reviewer_id}",
                    decision="APPROVED",
                )
            )
        if correction is None:
            article.status = "APPROVED"
        result_text = "Draft approved."
        audit_status = "APPROVED"
    else:
        db.add(
            ReviewDecision(
                draft_id=draft.id,
                reviewer_id=f"telegram:{reviewer_id}",
                decision="REJECTED",
            )
        )
        if correction is None:
            article.status = "REJECTED"
        result_text = "Draft rejected."
        audit_status = "REJECTED"

    db.add(
        TelegramCallback(
            callback_query_id=callback.id,
            reviewer_id=reviewer_id,
            action=action,
            draft_id=draft.id,
        )
    )
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=draft.id,
            operation=f"telegram_review_{action}",
            status=audit_status,
            actor=f"telegram:{reviewer_id}",
            details={"callback_query_id": callback.id},
        )
    )
    db.commit()
    if action == "approve":
        from app.api.routes.drafts import publish_draft

        try:
            delivery = await publish_draft(
                draft.id,
                request,
                db,
                f"telegram:{reviewer_id}",
            )
            result_text = f"Approved and published to {delivery.channel_id}."
            db.add(
                AuditEvent(
                    article_id=article.id,
                    draft_id=draft.id,
                    operation="telegram_approval_publish",
                    status="DELIVERED",
                    actor=f"telegram:{reviewer_id}",
                    details={"message_id": delivery.message_id},
                )
            )
            db.commit()
        except HTTPException as exc:
            result_text = f"Approved, but publishing failed: {str(exc.detail)[:120]}"
    await _answer_callback(settings, callback.id, result_text)
    return TelegramWebhookResponse(ok=True)


async def _answer_callback(settings: Settings, callback_id: str, text: str) -> None:
    try:
        await TelegramClient(settings).answer_callback(callback_id, text)
    except TelegramDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _handle_command(
    text: str,
    chat_id: str,
    reviewer_id: str,
    settings: Settings,
    db: Session,
    request: Request,
) -> None:
    command, _, argument = text.strip().partition(" ")
    command = command.split("@", maxsplit=1)[0].casefold()
    if command == "/help":
        reply = (
            "/status — operational state\n"
            "/queue — pending review items\n"
            "/pause [publishing|collection|all]\n"
            "/resume [publishing|collection|all]\n"
            "/run — start one bounded collection run\n"
            "/search <topic> — find a web article and prepare a post for approval\n"
            "/analyze <https-url> — analyze an article\n"
            "/verify <article-id> — verify claims and evidence\n"
            "/draft <draft-id> — view a draft\n"
            "/edit <draft-id> <text> — create a revised draft\n"
            "/approve <draft-id> or /reject <draft-id>\n"
            "/publish <draft-id> — publish an approved draft\n"
            "/sources — list configured sources\n"
            "/source <source-id> enable|disable\n"
            "/addsource <name>|RSS|<https-feed-url>\n"
            "/settings — view non-secret settings"
        )
    elif command == "/settings":
        reply = (
            f"Mode: {settings.publish_mode}\n"
            f"Auto-publishing enabled: {settings.auto_publish_enabled}\n"
            f"Daily post limit: {settings.max_posts_per_day}\n"
            f"Auto-publish categories: {', '.join(settings.auto_publish_categories) or 'none'}"
        )
    elif command == "/status":
        control = get_operations_control(db)
        latest = db.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1))
        pending = (
            db.scalar(select(func.count(Article.id)).where(Article.status == "PENDING_APPROVAL"))
            or 0
        )
        reply = (
            f"Mode: {settings.publish_mode}\n"
            f"Publishing paused: {control.publishing_paused}\n"
            f"Collection paused: {control.collection_paused}\n"
            f"Published today: {published_today_count(db)}/{settings.max_posts_per_day}\n"
            f"Pending review: {pending}\n"
            f"Latest collection run: {latest.status if latest else 'none'}"
        )
    elif command == "/queue":
        articles = db.scalars(
            select(Article)
            .where(Article.status == "PENDING_APPROVAL")
            .order_by(Article.discovered_at.desc())
            .limit(10)
        ).all()
        queue_lines = []
        for article in articles:
            latest_draft = max(article.drafts, key=lambda item: item.version, default=None)
            queue_lines.append(
                f"• {article.title}\n"
                f"  Article: {article.id}\n"
                f"  Draft: {latest_draft.id if latest_draft else 'none'}"
            )
        reply = (
            "Pending review:\n" + "\n".join(queue_lines)
            if articles
            else "The review queue is empty."
        )
    elif command == "/search":
        query = argument.strip()
        if len(query) < 3:
            reply = "Usage: /search <topic or question to find an article about>"
        elif len(query) > 300:
            reply = "Search requests must be 300 characters or fewer."
        elif get_operations_control(db).collection_paused:
            reply = "Web discovery is paused by an administrator."
        else:
            try:
                async with request.app.state.collection_lock:
                    article, sent = await discover_web_query(db, settings, query)
                if article is None:
                    reply = "No usable new article found. Try a more specific search."
                elif sent:
                    reply = (
                        f"Prepared a post for your request: {article.title}\n"
                        "I sent the source-linked draft to the review chat. Approve and publish "
                        "or reject it there."
                    )
                else:
                    reply = (
                        f"Analyzed {article.title}, but could not send the approval preview. "
                        "Check Telegram delivery settings and try again."
                    )
            except AnalysisUnavailable as exc:
                reply = f"Web search unavailable: {exc}"
    elif command == "/analyze":
        if not argument.strip():
            reply = "Usage: /analyze <https-url>"
        else:
            try:
                article, analysis, draft, recommendation = await analyze_and_persist(
                    db,
                    settings,
                    url=argument.strip(),
                    text=None,
                    title=None,
                    category_hint=None,
                    language="en",
                )
                reply = (
                    f"Analysis complete: {article.title}\n"
                    f"Category: {article.category} | Importance: {analysis.importance:.2f}/10\n"
                    f"Recommendation: {recommendation}\n"
                    f"Article ID: {article.id}\nDraft ID: {draft.id}"
                )
                if analysis.warnings or draft.quality_findings:
                    reply += "\nNotes: " + "; ".join(
                        (analysis.warnings + draft.quality_findings)[:3]
                    )
            except DuplicateArticleError as exc:
                reply = f"Article already exists. Article ID: {exc.article.id}"
            except (ArticleExtractionError, ValueError) as exc:
                reply = f"Could not analyze the article: {exc}"
            except (AnalysisInvalidResponse, AnalysisUnavailable) as exc:
                reply = f"Analysis provider failed: {exc}"
    elif command == "/verify":
        article_id = argument.strip()
        article = db.get(Article, article_id) if article_id else None
        if article is None:
            reply = "Usage: /verify <existing-article-id>"
        else:
            try:
                async with asyncio.timeout(settings.max_verification_duration_seconds):
                    run = await VerificationService(settings, db).verify(article)
                claim_lines = [
                    f"- {claim.status}: {claim.text[:180]}" for claim in run.claims[:5]
                ]
                reply = (
                    f"Verification: {run.status} ({run.verification_score:.0%})\n"
                    f"Article ID: {article.id}\n"
                    + ("\n".join(claim_lines) if claim_lines else "No claims were extracted.")
                )
                if run.warnings:
                    reply += "\nNotes: " + "; ".join(run.warnings[:3])
            except TimeoutError:
                db.rollback()
                reply = "Verification timed out. No verified result was produced."
            except AnalysisInvalidResponse as exc:
                reply = f"Verification provider returned an invalid response: {exc}"
            except AnalysisUnavailable as exc:
                reply = f"Verification provider failed: {exc}"
    elif command == "/draft":
        draft = db.get(Draft, argument.strip()) if argument.strip() else None
        if draft is None:
            reply = "Usage: /draft <existing-draft-id>"
        else:
            article = db.get(Article, draft.article_id)
            title = article.title if article else "Article"
            reply = (
                f"{title}\nDraft {draft.id} (v{draft.version}, {draft.quality_status})\n\n"
                f"{draft.body}"
            )
            if draft.quality_findings:
                reply += "\n\nReview notes: " + "; ".join(draft.quality_findings[:3])
    elif command == "/edit":
        draft_id, separator, body = argument.strip().partition(" ")
        if not separator or not body.strip():
            reply = "Usage: /edit <draft-id> <replacement draft text>"
        else:
            from app.api.routes.drafts import edit_draft

            try:
                result = edit_draft(
                    draft_id,
                    EditDraftRequest(body=body.strip(), comment="Edited via Telegram"),
                    request,
                    db,
                    f"telegram:{reviewer_id}",
                )
                reply = (
                    f"Draft revision created: {result.id} (v{result.version})\n"
                    f"Quality: {result.quality_status}"
                )
                if result.quality_findings:
                    reply += "\nReview notes: " + "; ".join(result.quality_findings[:3])
            except HTTPException as exc:
                reply = f"Draft edit failed: {exc.detail}"
            except ValueError as exc:
                reply = f"Draft edit failed: {exc}"
    elif command in {"/approve", "/reject"}:
        from app.api.routes.drafts import approve_draft, reject_draft

        draft_id = argument.strip()
        try:
            result = (
                approve_draft(
                    draft_id,
                    ReviewRequest(comment="Approved via Telegram"),
                    db,
                    f"telegram:{reviewer_id}",
                )
                if command == "/approve"
                else reject_draft(
                    draft_id,
                    ReviewRequest(comment="Rejected via Telegram"),
                    db,
                    f"telegram:{reviewer_id}",
                )
            )
            reply = f"Draft {result.decision.lower()}: {draft_id}"
            if command == "/approve":
                from app.api.routes.drafts import publish_draft

                try:
                    delivery = await publish_draft(
                        draft_id,
                        request,
                        db,
                        f"telegram:{reviewer_id}",
                    )
                    reply += f"\nPublished to {delivery.channel_id}."
                except HTTPException as exc:
                    reply += f"\nApproved, but publishing failed: {str(exc.detail)[:120]}"
        except HTTPException as exc:
            reply = f"Review failed: {exc.detail}"
    elif command == "/publish":
        from app.api.routes.drafts import publish_draft

        try:
            result = await publish_draft(
                argument.strip(),
                request,
                db,
                f"telegram:{reviewer_id}",
            )
            reply = (
                f"Published draft {result.draft_id} to {result.channel_id} "
                f"(message {result.message_id})."
            )
        except HTTPException as exc:
            reply = f"Publish failed: {exc.detail}"
    elif command == "/sources":
        sources = db.scalars(select(Source).order_by(Source.name).limit(20)).all()
        reply = (
            "Sources:\n"
            + "\n".join(
                f"- {source.name} ({source.source_type}): "
                f"{'enabled' if source.enabled else 'disabled'} — {source.id}"
                for source in sources
            )
            if sources
            else "No sources are configured."
        )
    elif command == "/addsource":
        name, separator, remainder = argument.strip().partition("|")
        source_type, type_separator, base_url = remainder.partition("|")
        if not separator or not type_separator or not name.strip() or not base_url.strip():
            reply = "Usage: /addsource <name>|RSS|<https-feed-url>"
        else:
            try:
                result = await register_source(
                    SourceCreateRequest(
                        name=name.strip(),
                        source_type=source_type.strip().upper(),
                        base_url=base_url.strip(),
                    ),
                    db,
                    f"telegram:{reviewer_id}",
                )
                reply = (
                    f"Source added: {result.name} ({result.source_type})\n"
                    f"ID: {result.id}\n"
                    "Scheduled scans will check it according to its configured interval."
                )
            except HTTPException as exc:
                reply = f"Could not add source: {exc.detail}"
            except ValueError as exc:
                reply = f"Invalid source: {exc}"
    elif command == "/source":
        source_id, separator, action = argument.strip().partition(" ")
        source = db.get(Source, source_id) if source_id else None
        if source is None or not separator or action.strip().lower() not in {
            "enable",
            "disable",
        }:
            reply = "Usage: /source <source-id> enable|disable"
        else:
            enabled = action.strip().lower() == "enable"
            source.enabled = enabled
            if enabled:
                source.last_error = None
            db.add(
                AuditEvent(
                    operation="telegram_source_updated",
                    status="ENABLED" if enabled else "DISABLED",
                    actor=f"telegram:{reviewer_id}",
                    details={"source_id": source.id},
                )
            )
            db.commit()
            reply = f"Source {source.name} {'enabled' if enabled else 'disabled'}."
    elif command in {"/pause", "/resume"}:
        scope = argument.strip().lower() or "all"
        if scope not in {"publishing", "collection", "all"}:
            reply = "Use: /pause [publishing|collection|all] or /resume [publishing|collection|all]"
        else:
            control = get_operations_control(db)
            paused = command == "/pause"
            if scope in {"publishing", "all"}:
                control.publishing_paused = paused
            if scope in {"collection", "all"}:
                control.collection_paused = paused
            control.updated_by = f"telegram:{reviewer_id}"
            control.reason = f"Telegram command from reviewer {reviewer_id}" if paused else None
            db.add(
                AuditEvent(
                    operation=f"telegram_{command}",
                    status="PAUSED" if paused else "RESUMED",
                    actor=f"telegram:{reviewer_id}",
                    details={"scope": scope},
                )
            )
            db.commit()
            reply = f"{scope.title()} operations {'paused' if paused else 'resumed'}."
    elif command == "/run":
        control = get_operations_control(db)
        if control.collection_paused:
            reply = "Collection is paused by an administrator."
        elif request.app.state.collection_lock.locked():
            reply = "A collection run is already in progress."
        else:
            try:
                web_discovered = 0
                async with request.app.state.collection_lock:
                    run = await execute_collection(
                        db,
                        settings,
                        trigger_type="MANUAL",
                        on_article_created=lambda article: send_article_for_review(
                            db,
                            article,
                            settings,
                        ),
                    )
                    if settings.web_discovery_enabled:
                        web_discovered = await discover_web_articles(db, settings)
                reply = (
                    f"Collection {run.status.lower()}: checked {run.sources_checked} sources, "
                    f"found {run.items_found} items, added {run.items_new}.\n"
                    f"Web discovery created {web_discovered} review draft(s)."
                )
            except Exception as exc:
                db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Collection command failed ({type(exc).__name__})",
                ) from exc
    else:
        reply = "Unknown command. Use /help."
    try:
        await TelegramClient(settings).send_message(
            chat_id,
            reply[: settings.max_telegram_message_length],
        )
    except TelegramDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
