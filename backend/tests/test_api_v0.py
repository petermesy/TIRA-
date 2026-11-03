import httpx
import respx
from app.core.config import Settings
from app.database.models import Article, AuditEvent, PublishedPost
from app.main import create_app
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token", "X-Reviewer-ID": "reviewer-1"}
ARTICLE_TEXT = (
    "An AI research lab released a new machine learning model. "
    "The announcement describes its training approach and benchmark results."
)


def client_and_factory() -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite://",
        admin_api_token=SecretStr("test-admin-token"),
        telegram_bot_token=SecretStr("test-telegram-token"),
        telegram_channel_id="@tira-test",
        telegram_review_chat_id="review-chat",
    )
    application = create_app(settings, engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return TestClient(application), factory


def test_manual_analysis_persists_article_draft_and_audit() -> None:
    client, factory = client_and_factory()
    with client:
        response = client.post(
            "/api/v1/articles/analyze",
            json={"title": "AI research model release", "text": ARTICLE_TEXT},
        )
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["category"] == "AI"
        assert result["scores"]["importance"] == 4.65
        assert result["status"] == "PENDING_APPROVAL"
        assert result["quality_status"] == "PASS"
        article_response = client.get(f"/api/v1/articles/{result['article_id']}")
        assert article_response.status_code == 200
        assert article_response.json()["analysis"]["model_name"] == "deterministic-fallback"
        draft_response = client.get(f"/api/v1/articles/{result['article_id']}/draft")
        assert draft_response.status_code == 200
        assert draft_response.json()["id"] == result["draft_id"]
    with factory() as db:
        assert len(db.scalars(select(Article)).all()) == 1
        assert len(db.scalars(select(AuditEvent)).all()) == 1


def test_health_endpoint_reports_database_and_manual_mode() -> None:
    client, _ = client_and_factory()
    with client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "llm_configured": False,
        "telegram_configured": True,
        "publish_mode": "manual",
    }


def test_duplicate_text_submission_returns_existing_article() -> None:
    client, factory = client_and_factory()
    payload = {"title": "AI research model release", "text": ARTICLE_TEXT}
    with client:
        first = client.post("/api/v1/articles/analyze", json=payload)
        second = client.post("/api/v1/articles/analyze", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["article_id"] == second.json()["article_id"]
    with factory() as db:
        assert len(db.scalars(select(Article)).all()) == 1


def test_privileged_actions_require_admin_token() -> None:
    client, _ = client_and_factory()
    with client:
        analyzed = client.post(
            "/api/v1/articles/analyze",
            json={"title": "AI research model release", "text": ARTICLE_TEXT},
        ).json()
        response = client.post(f"/api/v1/drafts/{analyzed['draft_id']}/approve", json={})
        publish_response = client.post(f"/api/v1/drafts/{analyzed['draft_id']}/publish")
    assert response.status_code == 401
    assert publish_response.status_code == 401


def test_publish_requires_approval_and_is_idempotent_after_delivery() -> None:
    client, factory = client_and_factory()
    with client, respx.mock(assert_all_called=False) as mock:
        telegram = mock.post("https://api.telegram.org/bottest-telegram-token/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        )
        analyzed = client.post(
            "/api/v1/articles/analyze",
            json={"title": "AI research model release", "text": ARTICLE_TEXT},
        ).json()
        draft_id = analyzed["draft_id"]
        before_approval = client.post(f"/api/v1/drafts/{draft_id}/publish", headers=ADMIN_HEADERS)
        assert before_approval.status_code == 409
        approved = client.post(f"/api/v1/drafts/{draft_id}/approve", headers=ADMIN_HEADERS, json={})
        assert approved.status_code == 200
        published = client.post(f"/api/v1/drafts/{draft_id}/publish", headers=ADMIN_HEADERS)
        repeated = client.post(f"/api/v1/drafts/{draft_id}/publish", headers=ADMIN_HEADERS)
        assert published.status_code == 200, published.text
        assert repeated.status_code == 200
        assert published.json()["message_id"] == "77"
        assert repeated.json()["message_id"] == "77"
        assert telegram.call_count == 1
    with factory() as db:
        post = db.scalar(select(PublishedPost))
        assert post is not None
        assert post.delivery_status == "DELIVERED"


def test_failed_telegram_delivery_is_persisted_for_manual_retry() -> None:
    client, factory = client_and_factory()
    with client, respx.mock:
        telegram = respx.post("https://api.telegram.org/bottest-telegram-token/sendMessage").mock(
            side_effect=[
                httpx.Response(503, json={"ok": False, "description": "temporary outage"}),
                httpx.Response(200, json={"ok": True, "result": {"message_id": 88}}),
            ]
        )
        analyzed = client.post(
            "/api/v1/articles/analyze",
            json={"title": "AI research model release", "text": ARTICLE_TEXT},
        ).json()
        draft_id = analyzed["draft_id"]
        approval = client.post(f"/api/v1/drafts/{draft_id}/approve", headers=ADMIN_HEADERS, json={})
        assert approval.status_code == 200
        response = client.post(f"/api/v1/drafts/{draft_id}/publish", headers=ADMIN_HEADERS)
        assert response.status_code == 502
        retry = client.post(f"/api/v1/drafts/{draft_id}/publish", headers=ADMIN_HEADERS)
        assert retry.status_code == 200
        assert retry.json()["message_id"] == "88"
        assert telegram.call_count == 2
    with factory() as db:
        post = db.scalar(select(PublishedPost))
        article = db.scalar(select(Article))
        assert post is not None and post.delivery_status == "DELIVERED"
        assert post.error_class is None
        assert article is not None and article.status == "PUBLISHED"
