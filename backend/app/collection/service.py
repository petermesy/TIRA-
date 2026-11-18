import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import Analysis, Article, CollectionRun, Source
from app.intelligence.normalizer import classify_category
from app.services.article_service import DuplicateArticleError, analyze_and_persist
from app.sources.connectors.arxiv import ArxivConnector
from app.sources.connectors.contracts import ConnectorError, SourceBatch
from app.sources.connectors.github_releases import GitHubReleasesConnector
from app.sources.connectors.rss import RSSConnector

MAX_CONSECUTIVE_SOURCE_FAILURES = 5
MIN_CANDIDATE_TEXT_CHARS = 40
PROMOTIONAL_PHRASES = (
    "buy now",
    "limited time offer",
    "sign up today",
    "sponsored content",
)


def _connector(source: Source, settings: Settings):
    connectors = {
        "RSS": RSSConnector,
        "ARXIV": ArxivConnector,
        "GITHUB_RELEASES": GitHubReleasesConnector,
    }
    connector_type = connectors.get(source.source_type)
    if connector_type is None:
        raise ConnectorError(
            "Source connector type is not supported",
            error_class="UnsupportedConnector",
        )
    return connector_type(settings)


def _candidate_filter_reason(
    *,
    source: Source,
    title: str,
    excerpt: str,
    published_at: datetime | None,
    language: str,
    now: datetime,
    max_age_days: int,
) -> str | None:
    if not title.strip() or len(excerpt.strip()) < MIN_CANDIDATE_TEXT_CHARS:
        return "missing_title_or_insufficient_content"
    if language.lower().split("-", 1)[0] != "en":
        return "unsupported_language"
    if published_at:
        published_utc = (
            published_at.astimezone(UTC)
            if published_at.tzinfo
            else published_at.replace(tzinfo=UTC)
        )
        if published_utc < now - timedelta(days=max_age_days):
            return "outside_recency_window"
        if published_utc > now + timedelta(days=1):
            return "publication_date_in_future"
    content = f"{title} {excerpt}".lower()
    if any(phrase in content for phrase in PROMOTIONAL_PHRASES):
        return "promotional_content"
    category = classify_category(content)
    if source.categories and category.value not in source.categories:
        return "category_not_allowed"
    return None


def _daily_analysis_count(db: Session, now: datetime) -> int:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.scalar(select(func.count(Analysis.id)).where(Analysis.created_at >= midnight)) or 0
    )


def _record_error(run: CollectionRun, error: dict[str, str]) -> None:
    run.errors = [*run.errors, error]


def _record_metrics(run: CollectionRun, metrics: dict[str, object]) -> None:
    run.source_metrics = [*run.source_metrics, metrics]


async def execute_collection(
    db: Session,
    settings: Settings,
    *,
    trigger_type: str = "MANUAL",
    on_article_created: Callable[[Article], Awaitable[None]] | None = None,
) -> CollectionRun:
    started_clock = time.monotonic()
    started_at = datetime.now(UTC)
    run = CollectionRun(
        trigger_type=trigger_type,
        status="RUNNING",
        started_at=started_at,
        errors=[],
        source_metrics=[],
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    sources = db.scalars(
        select(Source).where(Source.enabled.is_(True)).order_by(Source.created_at)
    ).all()
    source_limit = min(
        settings.max_collection_sources,
        max(1, settings.max_source_requests_per_run // 8),
    )
    sources = sources[:source_limit]
    run.sources_checked = 0
    daily_count = _daily_analysis_count(db, started_at)
    if settings.llm_configured:
        analysis_budget = min(
            settings.max_llm_items_per_run,
            max(0, settings.max_llm_items_per_day - daily_count),
        )
    else:
        analysis_budget = settings.max_collection_items_per_run
    remaining_items = min(settings.max_collection_items_per_run, analysis_budget)
    run.errors = []
    run.source_metrics = []
    db.commit()

    stop_after = time.monotonic() + settings.max_collection_duration_seconds
    had_error = False
    for source in sources:
        if remaining_items <= 0 or time.monotonic() >= stop_after:
            break
        now = datetime.now(UTC)
        if trigger_type == "SCHEDULED" and source.last_checked_at:
            last_checked = (
                source.last_checked_at.astimezone(UTC)
                if source.last_checked_at.tzinfo
                else source.last_checked_at.replace(tzinfo=UTC)
            )
            if last_checked > now - timedelta(minutes=source.interval_minutes):
                continue
        item_limit = min(source.max_items_per_run, remaining_items)
        metrics = {
            "source_id": source.id,
            "source_name": source.name,
            "status": "RUNNING",
            "items_found": 0,
            "items_new": 0,
            "duplicates": 0,
            "filtered": 0,
            "requests_made": 0,
        }
        source.last_checked_at = now
        run.sources_checked += 1
        db.commit()
        try:
            connector = _connector(source, settings)
            remaining_seconds = stop_after - time.monotonic()
            if remaining_seconds <= 0:
                metrics["status"] = "BUDGET_EXHAUSTED"
                _record_metrics(run, metrics)
                break
            batch: SourceBatch = await asyncio.wait_for(
                connector.fetch(source, cursor=None, limit=item_limit),
                timeout=remaining_seconds,
            )
            metrics["requests_made"] = batch.requests_made
            run.items_found += len(batch.items)
            metrics["items_found"] = len(batch.items)
            run.filtered += len(batch.items) - min(len(batch.items), item_limit)

            for candidate in batch.items:
                if remaining_items <= 0 or time.monotonic() >= stop_after:
                    break
                reason = _candidate_filter_reason(
                    source=source,
                    title=candidate.title,
                    excerpt=candidate.content_excerpt,
                    published_at=candidate.published_at,
                    language=candidate.language,
                    now=now,
                    max_age_days=settings.max_article_age_days,
                )
                if reason:
                    run.filtered += 1
                    metrics["filtered"] += 1
                    continue
                try:
                    article, _, _, _ = await asyncio.wait_for(
                        analyze_and_persist(
                            db,
                            settings,
                            url=str(candidate.url),
                            text=candidate.content_excerpt,
                            title=candidate.title,
                            category_hint=None,
                            language=candidate.language,
                            source_name=candidate.source_name,
                            published_at=candidate.published_at,
                            author=candidate.author,
                            image_url=str(candidate.image_url) if candidate.image_url else None,
                            retrieval_method=candidate.retrieval_method,
                            registered_source_id=source.id,
                        ),
                        timeout=max(0.1, stop_after - time.monotonic()),
                    )
                except DuplicateArticleError:
                    run.duplicates += 1
                    metrics["duplicates"] += 1
                    continue
                except TimeoutError:
                    _record_error(
                        run,
                        {"source_id": source.id, "error_class": "CollectionTimeBudgetExceeded"},
                    )
                    metrics["status"] = "PARTIAL"
                    had_error = True
                    break
                except Exception as exc:
                    db.rollback()
                    _record_error(
                        run,
                        {
                            "source_id": source.id,
                            "article_id": "",
                            "error_class": type(exc).__name__,
                        },
                    )
                    metrics["status"] = "PARTIAL"
                    had_error = True
                    continue
                if article.status == "PENDING_APPROVAL":
                    run.items_new += 1
                    run.items_analyzed += 1
                    metrics["items_new"] += 1
                    remaining_items -= 1
                    if on_article_created is not None:
                        await on_article_created(article)

            for warning in batch.warnings:
                _record_error(
                    run,
                    {
                        "source_id": source.id,
                        "error_class": "MalformedSourceRecord",
                        "message": warning[:200],
                    },
                )
            source.consecutive_failures = 0
            source.last_success_at = datetime.now(UTC)
            source.last_error = None
            if metrics["status"] == "RUNNING":
                metrics["status"] = "COMPLETED"
        except TimeoutError:
            had_error = True
            metrics["status"] = "FAILED"
            error_class = "SourceTimeout"
            _record_error(run, {"source_id": source.id, "error_class": error_class})
            source.consecutive_failures += 1
            source.last_error = error_class
        except ConnectorError as exc:
            had_error = True
            metrics["status"] = "FAILED"
            _record_error(run, {"source_id": source.id, "error_class": exc.error_class})
            source.consecutive_failures += 1
            source.last_error = exc.error_class
        except Exception as exc:
            had_error = True
            metrics["status"] = "FAILED"
            error_class = type(exc).__name__
            _record_error(run, {"source_id": source.id, "error_class": error_class})
            source.consecutive_failures += 1
            source.last_error = error_class

        if source.consecutive_failures >= MAX_CONSECUTIVE_SOURCE_FAILURES:
            source.enabled = False
            _record_error(run, {"source_id": source.id, "error_class": "SourceAutoDisabled"})
        _record_metrics(run, metrics)
        db.commit()

    run.finished_at = datetime.now(UTC)
    run.duration_ms = int((time.monotonic() - started_clock) * 1000)
    if remaining_items <= 0 and settings.llm_configured and analysis_budget <= 0:
        run.status = "BUDGET_EXHAUSTED"
    else:
        run.status = "PARTIAL" if had_error else "COMPLETED"
    db.commit()
    db.refresh(run)
    return run


def collection_run_response(run: CollectionRun) -> dict[str, object]:
    return {
        "id": run.id,
        "status": run.status,
        "trigger_type": run.trigger_type,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "sources_checked": run.sources_checked,
        "items_found": run.items_found,
        "items_new": run.items_new,
        "duplicates": run.duplicates,
        "filtered": run.filtered,
        "items_analyzed": run.items_analyzed,
        "duration_ms": run.duration_ms,
        "errors": run.errors,
        "source_metrics": run.source_metrics,
    }
