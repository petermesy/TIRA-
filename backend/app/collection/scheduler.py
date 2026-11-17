import asyncio
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collection.service import execute_collection
from app.core.config import Settings
from app.database.models import (
    Analysis,
    Article,
    AuditEvent,
    Draft,
    VerificationRun,
    VerifiedClaim,
)
from app.intelligence.analyzer.service import AnalysisInvalidResponse, AnalysisUnavailable
from app.services.article_service import DuplicateArticleError, analyze_and_persist
from app.services.operations import get_operations_control
from app.sources.manual.url_reader import ArticleExtractionError
from app.telegram.client import TelegramClient, TelegramDeliveryError
from app.verification.service import VerificationService

logger = logging.getLogger("tira.collection.scheduler")
WEB_DISCOVERY_QUERIES = (
    "engaging science facts explained physics relativity astronomy biology",
    "latest artificial intelligence AI research technology news",
    "latest space exploration astronomy space technology news",
    "latest software development programming open source technology news",
    "recent scientific discoveries explained by researchers",
)


async def send_article_for_review(db: Session, article: Article, settings: Settings) -> bool:
    draft = db.scalar(
        select(Draft)
        .where(Draft.article_id == article.id)
        .order_by(Draft.version.desc())
        .limit(1)
    )
    if draft is None:
        logger.error("No draft found for scheduled article %s", article.id)
        return False
    analysis = db.scalar(
        select(Analysis)
        .where(Analysis.article_id == article.id)
        .order_by(Analysis.created_at.desc())
        .limit(1)
    )
    verification = db.scalar(
        select(VerificationRun)
        .where(VerificationRun.article_id == article.id)
        .order_by(VerificationRun.version.desc())
        .limit(1)
    )
    lines = [
        "TIRA — CHANNEL POST PREVIEW — APPROVAL REQUIRED",
        f"Title: {article.title}",
        f"Category: {article.category}",
        f"Importance: {analysis.importance:.2f}/10" if analysis else "Importance: unavailable",
        f"Quality: {draft.quality_status}",
    ]
    if analysis:
        lines.append(f"Prepared by: {analysis.provider} / {analysis.model_name}")
    if verification:
        lines.append(
            f"Verification: {verification.status} ({verification.verification_score:.0%})"
        )
        claims = db.scalars(
            select(VerifiedClaim)
            .where(VerifiedClaim.verification_run_id == verification.id)
            .order_by(VerifiedClaim.claim_key)
        ).all()
        lines.extend(f"- {claim.status}: {claim.text[:160]}" for claim in claims[:3])
    else:
        lines.append("Verification: NOT RUN")
    if article.canonical_url:
        lines.append(f"Source: {article.canonical_url}")
    lines.extend(["", "POST PREVIEW:", draft.body, "", f"Draft ID: {draft.id}"])
    markup = {
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
            "\n".join(lines)[: settings.max_telegram_message_length],
            reply_markup=markup,
        )
    except TelegramDeliveryError as exc:
        db.add(
            AuditEvent(
                article_id=article.id,
                draft_id=draft.id,
                operation="telegram_scheduled_review",
                status="FAILED",
                actor="system:scheduler",
                details={"error_class": exc.error_class},
            )
        )
        db.commit()
        logger.error(
            "Could not send scheduled review for article %s (%s)",
            article.id,
            exc.error_class,
        )
        return False
    db.add(
        AuditEvent(
            article_id=article.id,
            draft_id=draft.id,
            operation="telegram_scheduled_review",
            status="DELIVERED",
            actor="system:scheduler",
            details={"message_id": message_id},
        )
    )
    db.commit()
    return True


async def discover_web_query(
    db: Session,
    settings: Settings,
    query: str,
) -> tuple[Article | None, bool]:
    if not settings.searxng_search_configured:
        raise AnalysisUnavailable("SearXNG web search is not configured")
    if not settings.llm_configured:
        raise AnalysisUnavailable("LLM article analysis is not configured")

    search = VerificationService(settings, db)
    results = await search._searxng_search(query[:300])
    seen_urls: set[str] = set()
    for result in results[:3]:
        url = result.get("url", "")
        if not url.startswith("https://") or url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            article, _, _, _ = await analyze_and_persist(
                db,
                settings,
                url=url,
                text=None,
                title=result.get("title") or None,
                category_hint=None,
                language="en",
            )
        except DuplicateArticleError:
            continue
        except (
            ArticleExtractionError,
            AnalysisInvalidResponse,
            AnalysisUnavailable,
            ValueError,
        ) as exc:
            logger.info("Interactive web candidate skipped (%s)", type(exc).__name__)
            db.rollback()
            continue
        return article, await send_article_for_review(db, article, settings)
    return None, False


async def discover_web_articles(db: Session, settings: Settings) -> int:
    if not settings.searxng_search_configured:
        logger.warning("Web discovery skipped: SEARXNG_BASE_URL is not configured")
        return 0
    if not settings.llm_configured:
        logger.warning("Web discovery skipped: LLM is not configured")
        return 0

    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    analyzed_today = int(
        db.scalar(select(func.count(Analysis.id)).where(Analysis.created_at >= midnight)) or 0
    )
    remaining_daily_budget = max(0, settings.max_llm_items_per_day - analyzed_today)
    item_budget = min(
        settings.max_web_discovery_items_per_run,
        settings.max_llm_items_per_run,
        remaining_daily_budget,
    )
    if item_budget <= 0:
        logger.info("Web discovery skipped: daily or per-run analysis budget is exhausted")
        return 0

    search = VerificationService(settings, db)
    seen_urls: set[str] = set()
    discovered = 0
    for query in WEB_DISCOVERY_QUERIES:
        try:
            results = await search._searxng_search(query)
        except AnalysisUnavailable as exc:
            logger.warning("Web discovery query failed (%s)", exc)
            continue
        for result in results:
            if discovered >= item_budget:
                return discovered
            url = result.get("url", "")
            if not url.startswith("https://") or url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                article, _, _, _ = await analyze_and_persist(
                    db,
                    settings,
                    url=url,
                    text=None,
                    title=result.get("title") or None,
                    category_hint=None,
                    language="en",
                )
            except DuplicateArticleError:
                continue
            except (
                ArticleExtractionError,
                AnalysisInvalidResponse,
                AnalysisUnavailable,
                ValueError,
            ) as exc:
                logger.info("Web candidate skipped (%s)", type(exc).__name__)
                db.rollback()
                continue
            await send_article_for_review(db, article, settings)
            discovered += 1
    return discovered


async def run_collection_scheduler(
    session_factory,
    settings: Settings,
    collection_lock: asyncio.Lock,
) -> None:
    logger.info(
        "Scheduled source monitoring enabled; polling every %s seconds",
        settings.collection_scheduler_interval_seconds,
    )
    next_web_search = time.monotonic()
    while True:
        await asyncio.sleep(settings.collection_scheduler_interval_seconds)
        with session_factory() as db:
            try:
                control = get_operations_control(db)
                if control.collection_paused:
                    logger.info("Scheduled source scan skipped because collection is paused")
                elif collection_lock.locked():
                    logger.info(
                        "Scheduled source scan skipped because collection is already running"
                    )
                else:
                    async with collection_lock:
                        await execute_collection(
                            db,
                            settings,
                            trigger_type="SCHEDULED",
                            on_article_created=lambda article: send_article_for_review(
                                db,
                                article,
                                settings,
                            ),
                        )
                        if (
                            settings.web_discovery_enabled
                            and time.monotonic() >= next_web_search
                        ):
                            discovered = await discover_web_articles(db, settings)
                            logger.info("Scheduled web discovery created %s drafts", discovered)
                            next_web_search = (
                                time.monotonic() + settings.web_discovery_interval_seconds
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                db.rollback()
                logger.exception("Scheduled source scan failed")
