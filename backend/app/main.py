import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.api.routes.articles import router as articles_router
from app.api.routes.collection import router as collection_router
from app.api.routes.drafts import router as drafts_router
from app.api.routes.operations import router as operations_router
from app.api.routes.telegram import router as telegram_router
from app.collection.scheduler import run_collection_scheduler
from app.core.config import Settings, get_settings
from app.core.rate_limit import InMemoryRateLimitMiddleware
from app.database import models as _models  # noqa: F401
from app.database.base import Base
from app.database.session import create_database_engine

logger = logging.getLogger("tira")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


class HealthResponse(BaseModel):
    status: str
    database: str
    llm_configured: bool
    telegram_configured: bool
    publish_mode: str


def create_app(
    settings: Settings | None = None,
    database_engine=None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    owns_engine = database_engine is None
    resolved_engine = database_engine or create_database_engine(resolved_settings)
    session_factory = sessionmaker(bind=resolved_engine, autoflush=False, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        scheduler_task = None
        if resolved_settings.app_env in {"development", "test"}:
            Base.metadata.create_all(bind=resolved_engine)
        if resolved_settings.collection_scheduler_enabled:
            if (
                not resolved_settings.telegram_configured
                or not resolved_settings.telegram_webhook_secret
                or not resolved_settings.telegram_reviewer_ids
            ):
                raise RuntimeError(
                    "Scheduled collection requires Telegram bot, channel, review chat, "
                    "webhook secret, and reviewer IDs to deliver approval requests"
                )
            scheduler_task = asyncio.create_task(
                run_collection_scheduler(
                    session_factory,
                    resolved_settings,
                    application.state.collection_lock,
                )
            )
        try:
            yield
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    pass
            if owns_engine:
                resolved_engine.dispose()

    application = FastAPI(
        title="TIRA API",
        version="0.1.0",
        description=(
            "Technology intelligence workflow with manual analysis and bounded source discovery "
            "claim-level evidence verification, and controlled publication (V0/V1/V2/V3)."
        ),
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.engine = resolved_engine
    application.state.session_factory = session_factory
    application.state.collection_lock = asyncio.Lock()
    application.state.publish_lock = asyncio.Lock()
    application.add_middleware(
        InMemoryRateLimitMiddleware,
        requests_per_minute=resolved_settings.max_requests_per_minute,
    )
    application.include_router(articles_router)
    application.include_router(collection_router)
    application.include_router(drafts_router)
    application.include_router(operations_router)
    application.include_router(telegram_router)

    @application.get("/api/v1/health", response_model=HealthResponse, tags=["operations"])
    def health(request: Request) -> HealthResponse:
        db = request.app.state.session_factory()
        try:
            db.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            logger.error("Database health check failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database is unavailable",
            ) from exc
        finally:
            db.close()
        return HealthResponse(
            status="ok",
            database="ok",
            llm_configured=resolved_settings.llm_configured,
            telegram_configured=resolved_settings.telegram_configured,
            publish_mode=resolved_settings.publish_mode,
        )

    return application


app = create_app()
