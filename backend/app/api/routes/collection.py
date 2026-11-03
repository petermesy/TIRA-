from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.collection_schemas import (
    CollectionRunResponse,
    SourceCreateRequest,
    SourceResponse,
    SourceUpdateRequest,
)
from app.collection.service import collection_run_response, execute_collection
from app.core.config import Settings
from app.core.security import require_admin, validate_public_url
from app.database.models import CollectionRun, Source
from app.database.session import get_db

router = APIRouter(prefix="/api/v1", tags=["collection"])
ALLOWED_CATEGORIES = {"AI", "CYBERSECURITY", "SOFTWARE", "EMERGING_TECH", "SCIENCE"}


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _source_response(source: Source) -> SourceResponse:
    return SourceResponse(
        id=source.id,
        name=source.name,
        source_type=source.source_type,
        base_url=source.base_url,
        domain=source.domain,
        categories=source.categories,
        enabled=source.enabled,
        interval_minutes=source.interval_minutes,
        rate_limit_per_minute=source.rate_limit_per_minute,
        max_items_per_run=source.max_items_per_run,
        last_checked_at=source.last_checked_at,
        last_success_at=source.last_success_at,
        consecutive_failures=source.consecutive_failures,
        last_error=source.last_error,
    )


@router.post("/sources", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def register_source(
    payload: SourceCreateRequest,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> SourceResponse:
    del reviewer_id
    try:
        canonical_url = await validate_public_url(str(payload.base_url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Source URL is not allowed: {exc}") from exc
    parsed = urlsplit(canonical_url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=422, detail="Source URLs must use HTTPS")
    if payload.source_type == "ARXIV" and parsed.hostname not in {
        "export.arxiv.org",
        "arxiv.org",
    }:
        raise HTTPException(
            status_code=422, detail="arXiv connector only supports official arXiv hosts"
        )
    if payload.source_type == "GITHUB_RELEASES":
        segments = [part for part in parsed.path.split("/") if part]
        if (
            parsed.hostname != "api.github.com"
            or len(segments) != 4
            or segments[0] != "repos"
            or segments[3] != "releases"
        ):
            raise HTTPException(
                status_code=422,
                detail="GitHub release sources must use https://api.github.com/repos/{owner}/{repo}/releases",
            )

    domain = parsed.hostname
    if not domain:
        raise HTTPException(status_code=422, detail="Source URL must include a hostname")
    categories = payload.categories or sorted(ALLOWED_CATEGORIES)
    source = Source(
        name=payload.name.strip(),
        source_type=payload.source_type.value,
        base_url=canonical_url,
        domain=domain,
        enabled=payload.enabled,
        categories=categories,
        interval_minutes=payload.interval_minutes,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        max_items_per_run=payload.max_items_per_run,
    )
    db.add(source)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A source with this name and type is already registered",
        ) from exc
    db.refresh(source)
    return _source_response(source)


@router.get("/sources", response_model=list[SourceResponse])
def list_sources(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> list[SourceResponse]:
    sources = db.scalars(select(Source).order_by(Source.created_at)).all()
    return [_source_response(source) for source in sources]


@router.patch("/sources/{source_id}", response_model=SourceResponse)
def update_source(
    source_id: str,
    payload: SourceUpdateRequest,
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> SourceResponse:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    source.enabled = payload.enabled
    source.last_error = None if payload.enabled else source.last_error
    db.commit()
    db.refresh(source)
    return _source_response(source)


@router.post("/collection/run", response_model=CollectionRunResponse)
async def run_collection(
    request: Request,
    trigger_type: str = "MANUAL",
    db: Session = Depends(get_db),
    reviewer_id: str = Depends(require_admin),
) -> CollectionRunResponse:
    del reviewer_id
    if trigger_type not in {"MANUAL", "SCHEDULED"}:
        raise HTTPException(status_code=422, detail="trigger_type must be MANUAL or SCHEDULED")
    collection_lock = request.app.state.collection_lock
    if collection_lock.locked():
        raise HTTPException(status_code=409, detail="A collection run is already in progress")
    try:
        async with collection_lock:
            run = await execute_collection(db, _settings(request), trigger_type=trigger_type)
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Collection run failed ({type(exc).__name__})",
        ) from exc
    return CollectionRunResponse.model_validate(collection_run_response(run))


@router.get("/collection/runs/latest", response_model=CollectionRunResponse | None)
def get_latest_collection_run(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> CollectionRunResponse | None:
    run = db.scalar(select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1))
    if run is None:
        return None
    return CollectionRunResponse.model_validate(collection_run_response(run))


@router.get("/collection/runs/{run_id}", response_model=CollectionRunResponse)
def get_collection_run(
    run_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> CollectionRunResponse:
    run = db.get(CollectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Collection run not found")
    return CollectionRunResponse.model_validate(collection_run_response(run))
