from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.models import OperationsControl, PublishedPost


def get_operations_control(db: Session) -> OperationsControl:
    control = db.get(OperationsControl, "global")
    if control is None:
        control = OperationsControl(id="global", updated_by="system")
        db.add(control)
        db.commit()
        db.refresh(control)
    return control


def published_today_count(db: Session, now: datetime | None = None) -> int:
    current_time = now or datetime.now(UTC)
    midnight = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.scalar(
            select(func.count(PublishedPost.id)).where(
                PublishedPost.delivery_status == "DELIVERED",
                PublishedPost.published_at >= midnight,
            )
        )
        or 0
    )
