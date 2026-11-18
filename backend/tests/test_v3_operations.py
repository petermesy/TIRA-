import asyncio
import json

import httpx
import respx
from app.api.schemas import Category
from app.collection.scheduler import discover_web_articles, send_article_for_review
from app.core.config import Settings
from app.database.models import (
    Article,
    ClaimEvidence,
    Draft,
    PostCorrection,
    PublishedPost,
    ReviewDecision,
    Source,
    VerificationRun,
    VerifiedClaim,
)
from app.intelligence.writer.templates import build_draft
from app.main import create_app
from app.verification.service import VerificationService
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ADMIN_HEADERS = {"Authorization": "Bearer test-admin-token"}
ARTICLE_TEXT = (
    "An AI research lab released a new machine learning model. "
    "The announcement describes its training approach and benchmark results."
)
TELEGRAM_SEND = "https://api.telegram.org/bottest-telegram-token/sendMessage"
TELEGRAM_PHOTO = "https://api.telegram.org/bottest-telegram-token/sendPhoto"
TELEGRAM_ANSWER = "https://api.telegram.org/bottest-telegram-token/answerCallbackQuery"


def _client(**settings_overrides: object) -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    settings_values: dict[str, object] = {
        "_env_file": None,
        "app_env": "test",
        "database_url": "sqlite://",
        "admin_api_token": SecretStr("test-admin-token"),
        "telegram_bot_token": SecretStr("test-telegram-token"),
        "telegram_channel_id": "@tira-test",
        "telegram_review_chat_id": "-1001",
        "telegram_retry_base_seconds": 0,
    }
    settings_values.update(settings_overrides)
    settings = Settings(**settings_values)
    return TestClient(create_app(settings, engine)), sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )


def _analyze(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v1/articles/analyze",
        json={"title": "AI research model release", "text": ARTICLE_TEXT},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_review_card_includes_opaque_actions_and_allowlisted_callback_approves() -> None:
    client, factory = _client(
        telegram_webhook_secret=SecretStr("test-webhook-secret"),
        telegram_reviewer_ids=["321"],
    )
    with client, respx.mock:
        send = respx.post(TELEGRAM_SEND).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 41}})
        )
        answer = respx.post(TELEGRAM_ANSWER).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        analyzed = _analyze(client)
        preview = client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/preview",
            headers=ADMIN_HEADERS,
        )
        assert preview.status_code == 200, preview.text
        markup = send.calls[0].request.content.decode()
        assert f"approve:{analyzed['draft_id']}" in markup
        assert f"reject:{analyzed['draft_id']}" in markup

        payload = {
            "callback_query": {
                "id": "callback-1",
                "data": f"approve:{analyzed['draft_id']}",
                "from": {"id": 321},
                "message": {"chat": {"id": -1001}},
            }
        }
        callback = client.post(
            "/api/v1/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
            json=payload,
        )
        assert callback.status_code == 200, callback.text
        repeated = client.post(
            "/api/v1/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
            json=payload,
        )
        assert repeated.status_code == 200
        assert repeated.json()["duplicate"] is True
        assert answer.call_count == 2
    with factory() as db:
        decision = db.scalar(select(ReviewDecision))
        assert decision is not None
        assert decision.reviewer_id == "telegram:321"
        assert decision.decision == "APPROVED"
        delivery = db.scalar(select(PublishedPost))
        assert delivery is not None
        assert delivery.delivery_status == "DELIVERED"


def test_publish_sends_article_image_with_post_as_photo_caption() -> None:
    client, factory = _client()
    with client, respx.mock:
        photo = respx.post(TELEGRAM_PHOTO).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 51}})
        )
        text = respx.post(TELEGRAM_SEND).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 52}})
        )
        analyzed = _analyze(client)
        with factory() as db:
            article = db.get(Article, analyzed["article_id"])
            assert article is not None
            article.image_url = "https://images.example/lead.jpg"
            draft = db.get(Draft, analyzed["draft_id"])
            assert draft is not None
            expected_caption = draft.body
            db.commit()
        client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/approve",
            headers=ADMIN_HEADERS,
            json={},
        )

        published = client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/publish",
            headers=ADMIN_HEADERS,
        )

    assert published.status_code == 200, published.text
    payload = json.loads(photo.calls[0].request.content)
    assert payload["photo"] == "https://images.example/lead.jpg"
    assert payload["caption"] == expected_caption
    assert text.call_count == 0


def test_long_photo_post_replies_with_full_text_and_reuses_image_on_retry() -> None:
    client, factory = _client()
    with client, respx.mock:
        photo = respx.post(TELEGRAM_PHOTO).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 61}})
        )
        text = respx.post(TELEGRAM_SEND).mock(
            side_effect=[
                httpx.Response(503, json={"ok": False}),
                httpx.Response(200, json={"ok": True, "result": {"message_id": 62}}),
            ]
        )
        analyzed = _analyze(client)
        long_body = "A detailed article summary. " * 50
        with factory() as db:
            article = db.get(Article, analyzed["article_id"])
            assert article is not None
            article.image_url = "https://images.example/lead.jpg"
            draft = db.get(Draft, analyzed["draft_id"])
            assert draft is not None
            draft.body = long_body
            db.commit()
        client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/approve",
            headers=ADMIN_HEADERS,
            json={},
        )

        failed = client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/publish",
            headers=ADMIN_HEADERS,
        )
        retried = client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/publish",
            headers=ADMIN_HEADERS,
        )

    assert failed.status_code == 502
    assert retried.status_code == 200, retried.text
    photo_payload = json.loads(photo.calls[0].request.content)
    assert photo_payload["photo"] == "https://images.example/lead.jpg"
    assert "caption" not in photo_payload
    text_payload = json.loads(text.calls[0].request.content)
    assert text_payload["text"] == long_body
    assert text_payload["reply_parameters"] == {"message_id": 61}
    assert text_payload["disable_web_page_preview"] is True
    assert photo.call_count == 1


def test_emergency_pause_blocks_publication_until_admin_resumes() -> None:
    client, _ = _client()
    with client, respx.mock:
        respx.post(TELEGRAM_SEND).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 88}})
        )
        analyzed = _analyze(client)
        approved = client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/approve",
            headers=ADMIN_HEADERS,
            json={},
        )
        assert approved.status_code == 200
        paused = client.post(
            "/api/v1/operations/pause",
            headers=ADMIN_HEADERS,
            json={"scope": "publishing", "reason": "Incident response"},
        )
        assert paused.status_code == 200
        assert paused.json()["publishing_paused"] is True
        collection_pause = client.post(
            "/api/v1/operations/pause",
            headers=ADMIN_HEADERS,
            json={"scope": "collection", "reason": "Maintenance"},
        )
        assert collection_pause.status_code == 200
        blocked_collection = client.post(
            "/api/v1/collection/run",
            headers=ADMIN_HEADERS,
        )
        assert blocked_collection.status_code == 423
        blocked = client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/publish",
            headers=ADMIN_HEADERS,
        )
        assert blocked.status_code == 423
        client.post(
            "/api/v1/operations/resume",
            headers=ADMIN_HEADERS,
            json={"scope": "publishing", "reason": "Incident resolved"},
        )
        published = client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/publish",
            headers=ADMIN_HEADERS,
        )
        assert published.status_code == 200, published.text


def test_auto_publish_requires_all_opt_in_gates_and_confirmed_evidence() -> None:
    client, _ = _client(
        publish_mode="auto_with_gates",
        auto_publish_enabled=True,
        min_auto_importance_score=0,
        min_auto_verification_score=0,
        auto_publish_categories=["AI"],
    )
    with client, respx.mock:
        telegram = respx.post(TELEGRAM_SEND).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 89}})
        )
        analyzed = _analyze(client)
        response = client.post(
            f"/api/v1/drafts/{analyzed['draft_id']}/publish",
            headers=ADMIN_HEADERS,
        )
        assert response.status_code == 409
        assert "No claim verification is available" in str(response.json())
        assert telegram.call_count == 0


def test_auto_publish_job_publishes_only_fully_confirmed_eligible_items() -> None:
    client, factory = _client(
        publish_mode="auto_with_gates",
        auto_publish_enabled=True,
        min_auto_importance_score=0,
        min_auto_verification_score=0,
        auto_publish_categories=["AI"],
    )
    with client, respx.mock:
        telegram = respx.post(TELEGRAM_SEND).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 121}})
        )
        analyzed = _analyze(client)
        with factory() as db:
            article = db.get(Article, analyzed["article_id"])
            assert article is not None
            verification = VerificationRun(
                article_id=article.id,
                version=1,
                status="CONFIRMED",
                verification_score=0.99,
                importance_score=8.8,
                importance_inputs={
                    "relevance": 9,
                    "novelty": 9,
                    "impact": 9,
                    "credibility": 9,
                    "technical_significance": 9,
                },
                importance_weights={
                    "relevance": 0.25,
                    "novelty": 0.20,
                    "impact": 0.20,
                    "credibility": 0.20,
                    "technical_significance": 0.15,
                },
                importance_policy_version="v2.1:AI",
                score_factors={
                    "primary_source_evidence": 1,
                    "independent_corroboration": 1,
                    "source_quality": 1,
                    "technical_evidence": 1,
                    "recency_and_context": 1,
                },
                policy_version="v2.1",
                provider="google_gemini",
                model_name="gemini-3.5-flash",
                warnings=[],
                cve_ids=[],
            )
            claim = VerifiedClaim(
                verification_run=verification,
                claim_key="c1",
                text="The research lab released a model.",
                is_material=True,
                status="CONFIRMED",
                verification_score=0.99,
                score_factors=verification.score_factors,
                rationale="Official and independent evidence agree.",
                search_queries=["research lab model release"],
                evidence=[
                    ClaimEvidence(
                        url="https://vendor.example/model",
                        publisher="vendor.example",
                        published_at=None,
                        retrieved_at=article.discovered_at,
                        excerpt="The research lab released the model.",
                        evidence_type="PRIMARY",
                        stance="SUPPORTS",
                        is_materially_misleading=False,
                        validated_cve_ids=[],
                        independence_group="vendor.example",
                        source_tier="AUTHORITATIVE",
                        content_hash="evidence-hash",
                    )
                ],
            )
            verification.claims.append(claim)
            db.add(verification)
            db.commit()
        response = client.post("/api/v1/operations/auto-publish", headers=ADMIN_HEADERS)
        assert response.status_code == 200, response.text
        assert response.json()["published"] == 1
        assert response.json()["outcomes"][0]["status"] == "DELIVERED"
        assert telegram.call_count == 1
    with factory() as db:
        decision = db.scalar(
            select(ReviewDecision).where(ReviewDecision.decision == "AUTO_APPROVED")
        )
        post = db.scalar(select(PublishedPost))
        assert decision is not None and decision.reviewer_id == "system:auto"
        assert post is not None and post.response_metadata["decision"] == "AUTO_APPROVED"


def test_allowlisted_telegram_commands_can_pause_and_report_status() -> None:
    client, _ = _client(
        telegram_webhook_secret=SecretStr("test-webhook-secret"),
        telegram_reviewer_ids=["321"],
    )
    with client, respx.mock:
        replies = respx.post(TELEGRAM_SEND).mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})
        )
        command = {
            "message": {
                "from": {"id": 321},
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "/pause publishing",
            }
        }
        response = client.post(
            "/api/v1/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
            json=command,
        )
        assert response.status_code == 200, response.text
        denied = client.post(
            "/api/v1/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
            json={
                "message": {
                    "from": {"id": 999},
                    "chat": {"id": -1001, "type": "supergroup"},
                    "text": "/status",
                }
            },
        )
        assert denied.status_code == 403
        status_response = client.get("/api/v1/operations/status", headers=ADMIN_HEADERS)
        assert status_response.status_code == 200
        assert status_response.json()["publishing_paused"] is True
        assert replies.call_count == 1


def test_telegram_admin_can_list_and_toggle_sources() -> None:
    client, factory = _client(
        telegram_webhook_secret=SecretStr("test-webhook-secret"),
        telegram_reviewer_ids=["321"],
    )
    with client:
        with factory() as db:
            source = Source(
                name="Research feed",
                source_type="RSS",
                base_url="https://research.example/feed.xml",
                domain="research.example",
                categories=["SCIENCE"],
            )
            db.add(source)
            db.commit()
            source_id = source.id

        with respx.mock:
            replies = respx.post(TELEGRAM_SEND).mock(
                return_value=httpx.Response(
                    200,
                    json={"ok": True, "result": {"message_id": 99}},
                )
            )
            headers = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
            listed = client.post(
                "/api/v1/telegram/webhook",
                headers=headers,
                json={
                    "message": {
                        "from": {"id": 321},
                        "chat": {"id": -1001, "type": "supergroup"},
                        "text": "/sources",
                    }
                },
            )
            assert listed.status_code == 200, listed.text
            toggled = client.post(
                "/api/v1/telegram/webhook",
                headers=headers,
                json={
                    "message": {
                        "from": {"id": 321},
                        "chat": {"id": -1001, "type": "supergroup"},
                        "text": f"/source {source_id} disable",
                    }
                },
            )
            assert toggled.status_code == 200, toggled.text
            assert replies.call_count == 2

    with factory() as db:
        assert db.get(Source, source_id).enabled is False


def test_scheduled_web_discovery_analyzes_candidates_and_sends_review(
    monkeypatch,
) -> None:
    client, factory = _client()
    with client:
        analyzed = _analyze(client)

    settings = Settings(
        _env_file=None,
        llm_provider="groq",
        llm_api_key=SecretStr("test-groq-key"),
        llm_model="openai/gpt-oss-20b",
        searxng_base_url="http://127.0.0.1:8888",
        max_web_discovery_items_per_run=1,
    )
    search_queries = []
    review_articles = []

    async def search(self, query: str) -> list[dict[str, str]]:
        search_queries.append(query)
        return [
            {"url": "https://news.example/ai-release", "title": "New AI release"},
            {"url": "https://news.example/space-discovery", "title": "Space discovery"},
        ]

    async def analyze(db, configured_settings, **kwargs):
        assert kwargs["url"] == "https://news.example/ai-release"
        article = db.get(Article, analyzed["article_id"])
        return article, None, None, "REVIEW"

    async def send_for_review(db, article, configured_settings) -> None:
        review_articles.append(article.id)

    monkeypatch.setattr(VerificationService, "_searxng_search", search)
    monkeypatch.setattr("app.collection.scheduler.analyze_and_persist", analyze)
    monkeypatch.setattr("app.collection.scheduler.send_article_for_review", send_for_review)

    with factory() as db:
        discovered = asyncio.run(discover_web_articles(db, settings))

    assert discovered == 1
    assert len(search_queries) == 1
    assert review_articles == [analyzed["article_id"]]


def test_telegram_review_contains_channel_ready_post_and_approval_actions() -> None:
    client, factory = _client()
    with client:
        analyzed = _analyze(client)
        with factory() as db, respx.mock:
            article = db.get(Article, analyzed["article_id"])
            send = respx.post(TELEGRAM_SEND).mock(
                return_value=httpx.Response(
                    200,
                    json={"ok": True, "result": {"message_id": 101}},
                )
            )
            asyncio.run(
                send_article_for_review(
                    db,
                    article,
                    client.app.state.settings,
                )
            )

        payload = send.calls[0].request.content.decode()
        assert "CHANNEL POST PREVIEW" in payload
        assert "POST PREVIEW:" in payload
        assert "Approve and publish" in payload
        assert f"approve:{analyzed['draft_id']}" in payload
        assert "reject:" + analyzed["draft_id"] in payload


def test_allowlisted_telegram_admin_can_search_a_custom_topic(monkeypatch) -> None:
    client, factory = _client(
        telegram_webhook_secret=SecretStr("test-webhook-secret"),
        telegram_reviewer_ids=["321"],
    )
    with client:
        analyzed = _analyze(client)
        with factory() as db:
            article = db.get(Article, analyzed["article_id"])

        queries = []

        async def discover(db, settings, query):
            queries.append(query)
            return article, True

        monkeypatch.setattr("app.api.routes.telegram.discover_web_query", discover)
        with respx.mock:
            response_send = respx.post(TELEGRAM_SEND).mock(
                return_value=httpx.Response(
                    200,
                    json={"ok": True, "result": {"message_id": 102}},
                )
            )
            response = client.post(
                "/api/v1/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
                json={
                    "message": {
                        "from": {"id": 321},
                        "chat": {"id": -1001, "type": "supergroup"},
                        "text": "/search how do quantum computers work?",
                    }
                },
            )

        assert response.status_code == 200, response.text
        assert queries == ["how do quantum computers work?"]
        assert "Prepared a post for your request" in response_send.calls[0].request.content.decode()


def test_science_draft_uses_full_article_summary_without_section_labels() -> None:
    summary = (
        "Did you know Google is exploring whether solar-powered satellites could support AI "
        "computing in space? Project Suncatcher is testing the idea while investigating the "
        "engineering challenges that must be solved."
    )
    body, format_type = build_draft(
        category=Category.SCIENCE,
        summary=summary,
        source_url="https://science.example/time",
        source_name="Science Example",
    )

    assert format_type == "research"
    assert body.startswith(f"🔬 SCIENCE FACT\n\n{summary}\n\n")
    assert summary in body
    assert "The science:" not in body
    assert "What happened:" not in body
    assert "Why it matters:" not in body
    assert "The project could change" not in body
    assert "https://science.example/time" in body
    assert "#ScienceFacts" in body


def test_ai_draft_uses_narrative_article_summary_without_generic_sections() -> None:
    summary = (
        "Did you know Google is testing whether solar-powered satellites could run AI "
        "computing workloads? Project Suncatcher is exploring that possibility and the "
        "technical challenges that still need to be solved."
    )
    body, _ = build_draft(
        category=Category.AI,
        summary=summary,
        source_url="https://ai.example/project",
        source_name="AI Example",
    )

    assert body.startswith(f"🤖 AI UPDATE\n\n{summary}\n\n")
    assert "Google explores AI computing in space" not in body
    assert "What happened:" not in body
    assert "Why it matters:" not in body


def test_scheduled_collection_starts_with_telegram_review_configuration() -> None:
    client, _ = _client(
        collection_scheduler_enabled=True,
        collection_scheduler_interval_seconds=60,
        telegram_webhook_secret=SecretStr("test-webhook-secret"),
        telegram_reviewer_ids=["321"],
    )
    with client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_telegram_delivery_retries_use_backoff_and_do_not_retry_permanent_errors() -> None:
    client, _ = _client(telegram_retry_base_seconds=30)
    with client, respx.mock:
        transient = respx.post(TELEGRAM_SEND).mock(
            return_value=httpx.Response(503, json={"ok": False})
        )
        analyzed = _analyze(client)
        draft_id = analyzed["draft_id"]
        client.post(
            f"/api/v1/drafts/{draft_id}/approve",
            headers=ADMIN_HEADERS,
            json={},
        )
        failed = client.post(
            f"/api/v1/drafts/{draft_id}/publish",
            headers=ADMIN_HEADERS,
        )
        assert failed.status_code == 502
        retry = client.post(
            f"/api/v1/drafts/{draft_id}/publish",
            headers=ADMIN_HEADERS,
        )
        assert retry.status_code == 429
        assert retry.headers["retry-after"]
        assert transient.call_count == 1

    permanent_client, _ = _client(telegram_retry_base_seconds=0)
    with permanent_client, respx.mock:
        permanent = respx.post(TELEGRAM_SEND).mock(
            return_value=httpx.Response(401, json={"ok": False})
        )
        analyzed = _analyze(permanent_client)
        draft_id = analyzed["draft_id"]
        permanent_client.post(
            f"/api/v1/drafts/{draft_id}/approve",
            headers=ADMIN_HEADERS,
            json={},
        )
        failed = permanent_client.post(
            f"/api/v1/drafts/{draft_id}/publish",
            headers=ADMIN_HEADERS,
        )
        assert failed.status_code == 502
        retry = permanent_client.post(
            f"/api/v1/drafts/{draft_id}/publish",
            headers=ADMIN_HEADERS,
        )
        assert retry.status_code == 409
        assert permanent.call_count == 1


def test_correction_is_linked_and_requires_review_before_delivery() -> None:
    client, factory = _client()
    with client, respx.mock:
        respx.post(TELEGRAM_SEND).mock(
            side_effect=[
                httpx.Response(200, json={"ok": True, "result": {"message_id": 91}}),
                httpx.Response(200, json={"ok": True, "result": {"message_id": 92}}),
            ]
        )
        analyzed = _analyze(client)
        draft_id = analyzed["draft_id"]
        client.post(
            f"/api/v1/drafts/{draft_id}/approve",
            headers=ADMIN_HEADERS,
            json={},
        )
        original = client.post(
            f"/api/v1/drafts/{draft_id}/publish",
            headers=ADMIN_HEADERS,
        )
        assert original.status_code == 200, original.text
        with factory() as db:
            original_post = db.scalar(select(PublishedPost))
            assert original_post is not None
            original_post_id = original_post.id
            db.commit()
        correction_response = client.post(
            f"/api/v1/drafts/published/{original_post_id}/correction",
            headers=ADMIN_HEADERS,
            json={
                "body": "Correction: Updated details are now available.",
                "reason": "A material detail in the original post was incorrect.",
            },
        )
        assert correction_response.status_code == 200, correction_response.text
        correction_draft_id = correction_response.json()["correction_draft_id"]
        blocked = client.post(
            f"/api/v1/drafts/{correction_draft_id}/publish",
            headers=ADMIN_HEADERS,
        )
        assert blocked.status_code == 409
        approved = client.post(
            f"/api/v1/drafts/{correction_draft_id}/approve",
            headers=ADMIN_HEADERS,
            json={},
        )
        assert approved.status_code == 200
        published = client.post(
            f"/api/v1/drafts/{correction_draft_id}/publish",
            headers=ADMIN_HEADERS,
        )
        assert published.status_code == 200, published.text
    with factory() as db:
        correction = db.scalar(select(PostCorrection))
        assert correction is not None
        assert correction.original_post_id == original_post_id
        assert correction.correction_draft_id == correction_draft_id
