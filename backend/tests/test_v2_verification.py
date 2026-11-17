import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from app.core.config import Settings
from app.database.base import Base
from app.database.models import Article, SourceCredibility
from app.intelligence.analyzer.service import AnalysisUnavailable
from app.main import create_app
from app.verification.policy import (
    EvidenceAssessment,
    classify_claim_evidence,
    score_claim_evidence,
)
from app.verification.service import UngroundedClaimError, VerificationService
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _evidence(
    *,
    evidence_type: str = "INDEPENDENT_SECONDARY",
    stance: str = "SUPPORTS",
    tier: str = "HIGH",
    group: str = "publisher.example",
    content_hash: str | None = None,
    published_at: datetime | None = None,
) -> EvidenceAssessment:
    return EvidenceAssessment(
        evidence_type=evidence_type,
        stance=stance,
        source_tier=tier,
        independence_group=group,
        content_hash=content_hash or group,
        published_at=published_at or datetime.now(UTC),
    )


def test_absent_or_unrated_evidence_is_unconfirmed_not_false() -> None:
    assert classify_claim_evidence([]) == "UNCONFIRMED"
    assert (
        classify_claim_evidence([_evidence(stance="CONTRADICTS", tier="UNASSESSED")])
        == "UNCONFIRMED"
    )


def test_reliable_support_and_contradiction_are_conflicting() -> None:
    evidence = [
        _evidence(evidence_type="PRIMARY", group="vendor.example"),
        _evidence(stance="CONTRADICTS", group="research.example"),
    ]
    assert classify_claim_evidence(evidence) == "CONFLICTING"


def test_false_requires_reliable_direct_contradiction() -> None:
    assert (
        classify_claim_evidence(
            [_evidence(evidence_type="PRIMARY", stance="CONTRADICTS", tier="AUTHORITATIVE")]
        )
        == "FALSE"
    )


def test_confirmation_requires_primary_and_independent_support() -> None:
    copied = [
        _evidence(evidence_type="PRIMARY", group="same-origin"),
        _evidence(group="same-origin"),
        _evidence(group="same-origin"),
    ]
    assert classify_claim_evidence(copied) == "SUPPORTED"
    assert (
        classify_claim_evidence(
            [
                _evidence(evidence_type="PRIMARY", group="vendor.example"),
                _evidence(group="independent.example"),
            ]
        )
        == "CONFIRMED"
    )


def test_near_duplicate_excerpts_are_not_independent_corroboration() -> None:
    evidence = [
        _evidence(
            evidence_type="PRIMARY",
            group="official.example",
            content_hash="primary",
        ),
        EvidenceAssessment(
            evidence_type="INDEPENDENT_SECONDARY",
            stance="SUPPORTS",
            source_tier="HIGH",
            independence_group="copied.example",
            content_hash="different-hash",
            published_at=datetime.now(UTC),
            excerpt="Researchers confirmed the tested model and published results.",
        ),
        EvidenceAssessment(
            evidence_type="INDEPENDENT_SECONDARY",
            stance="SUPPORTS",
            source_tier="HIGH",
            independence_group="syndicated.example",
            content_hash="another-hash",
            published_at=datetime.now(UTC),
            excerpt="Researchers confirmed the tested model and published results today.",
        ),
    ]
    assert score_claim_evidence(evidence)[1]["independent_corroboration"] == 0.5


def test_verification_score_is_bounded_reproducible_and_copy_aware() -> None:
    evidence = [
        _evidence(
            group="same-report",
            content_hash="copied-content",
            published_at=datetime.now(UTC) - timedelta(days=10),
        ),
        _evidence(
            group="same-report",
            content_hash="copied-content",
            published_at=datetime.now(UTC) - timedelta(days=10),
        ),
    ]
    score, factors = score_claim_evidence(evidence)
    repeated_score, repeated_factors = score_claim_evidence(evidence)
    assert score == repeated_score
    assert factors == repeated_factors
    assert 0 <= score <= 1
    assert factors["independent_corroboration"] == 0.5
    assert (
        score_claim_evidence(
            [
                _evidence(evidence_type="PRIMARY", group="vendor.example"),
                _evidence(group="news.example"),
            ]
        )[1]["independent_corroboration"]
        > 0
    )


def test_claim_grounding_accepts_minor_rephrasing_and_rejects_unsupported_details() -> None:
    source = (
        "The fictional Orion laboratory reports testing a ceramic sensor for 240 hours "
        "in a simulated lunar environment."
    )
    paraphrased = (
        "The Orion laboratory tested a ceramic sensor for 240 hours in a simulated lunar "
        "environment."
    )
    unsupported = "The Orion laboratory tested a ceramic sensor for 240 hours and cured cancer."

    assert VerificationService._claim_is_grounded(paraphrased, source)
    assert not VerificationService._claim_is_grounded(unsupported, source)


def test_gemini_verification_http_error_reports_status_without_exposing_key() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="google_gemini",
        llm_api_key=SecretStr("verification-test-secret"),
        llm_model="gemini-3.5-flash",
    )
    service = VerificationService(settings, None)

    with respx.mock:
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.5-flash:generateContent"
        ).mock(
            return_value=httpx.Response(
                403,
                json={
                    "error": {
                        "message": "Permission denied; token verification-test-secret",
                    }
                },
            )
        )
        with pytest.raises(AnalysisUnavailable) as error:
            asyncio.run(service._gemini_request({"contents": []}))

    assert "HTTP 403" in str(error.value)
    assert "Permission denied; token [REDACTED]" in str(error.value)
    assert "verification-test-secret" not in str(error.value)


def _article() -> Article:
    return Article(
        title="Vendor releases a tested AI model CVE-2026-12345",
        source_name="Original release",
        source_domain="release.example",
        category="AI",
        language="en",
        content_excerpt=(
            "The vendor released a tested AI model, identified as CVE-2026-12345, "
            "and published its technical evaluation."
        ),
        content_hash="article-hash",
        retrieval_method="RSS",
        status="PENDING_APPROVAL",
    )


def _candidate(text: str, *, grounding_chunks: list[dict[str, object]] | None = None) -> dict:
    content = {"parts": [{"text": text}]}
    candidate: dict[str, object] = {"content": content}
    if grounding_chunks is not None:
        candidate["groundingMetadata"] = {"groundingChunks": grounding_chunks}
    return {"candidates": [candidate]}


def test_verification_persists_claim_evidence_and_confirms_independent_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr("app.verification.service.validate_public_url", allow)
    monkeypatch.setattr("app.sources.manual.url_reader.validate_public_url", allow)

    model_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent"
    )
    claim = "The vendor released a tested AI model."
    primary_quote = "The vendor released a tested AI model after internal evaluation."
    secondary_quote = "Independent researchers tested the AI model."
    primary_page = (
        "<html><head><title>Vendor release</title></head><body>"
        f"<article>{primary_quote} CVE-2026-12345</article></body></html>"
    )
    secondary_page = (
        "<html><head><title>Independent review</title></head><body>"
        f"<article>{secondary_quote} The test methodology is published.</article></body></html>"
    )
    model_responses = [
        httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "claims": [
                                                {
                                                    "claim": claim,
                                                    "search_query": (
                                                        "tested AI model primary independent "
                                                        "evidence"
                                                    ),
                                                    "is_material": True,
                                                }
                                            ]
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        ),
        httpx.Response(
            200,
            json=_candidate(
                "Search results",
                grounding_chunks=[
                    {"web": {"uri": "https://vendor.example/model", "title": "Vendor"}},
                    {"web": {"uri": "https://research.example/model", "title": "Review"}},
                ],
            ),
        ),
        httpx.Response(
            200,
            json=_candidate(
                json.dumps(
                    {
                        "stance": "SUPPORTS",
                        "excerpt": primary_quote,
                        "rationale": "The vendor directly confirms the release.",
                    }
                )
            ),
        ),
        httpx.Response(
            200,
            json=_candidate(
                json.dumps(
                    {
                        "stance": "SUPPORTS",
                        "excerpt": secondary_quote,
                        "rationale": "Independent researchers report the test.",
                    }
                )
            ),
        ),
    ]
    settings = Settings(
        _env_file=None,
        llm_provider="google_gemini",
        llm_api_key=SecretStr("test-key"),
        llm_model="gemini-3.5-flash",
        max_verification_claims=4,
        max_evidence_per_claim=2,
    )
    with factory() as db:
        article = _article()
        db.add_all(
            [
                article,
                SourceCredibility(
                    domain="vendor.example",
                    tier="AUTHORITATIVE",
                    rationale="Official advisory and vendor-originated announcements.",
                    updated_by="reviewer",
                ),
                SourceCredibility(
                    domain="research.example",
                    tier="HIGH",
                    rationale="Independent research group with published methods.",
                    updated_by="reviewer",
                ),
            ]
        )
        db.commit()
        with respx.mock:
            respx.post(model_url).mock(side_effect=model_responses)
            respx.get("https://vendor.example/model").mock(
                return_value=httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text=primary_page,
                )
            )
            respx.get("https://research.example/model").mock(
                return_value=httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text=secondary_page,
                )
            )
            run = asyncio.run(VerificationService(settings, db).verify(article))

        assert run.status == "CONFIRMED"
        assert run.policy_version
        assert run.cve_ids == ["CVE-2026-12345"]
        assert run.claims[0].status == "CONFIRMED"
        assert len(run.claims[0].evidence) == 2
        assert run.cve_ids == ["CVE-2026-12345"]
        assert run.score_factors["primary_source_evidence"] == 1
        assert run.score_factors["independent_corroboration"] == 0.5
        assert run.claims[0].evidence[0].excerpt in primary_page + secondary_page


def test_unconfigured_verification_persists_unconfirmed_claims() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        article = _article()
        db.add(article)
        db.commit()
        run = asyncio.run(VerificationService(Settings(_env_file=None), db).verify(article))
        assert run.status == "UNCONFIRMED"
        assert run.provider == "deterministic"
        assert run.cve_ids == []
        assert run.claims
        assert all(claim.status == "UNCONFIRMED" for claim in run.claims)
        assert run.warnings


def test_ungrounded_llm_claims_fall_back_to_source_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        llm_provider="google_gemini",
        llm_api_key=SecretStr("test-key"),
        llm_model="gemini-3.5-flash",
    )

    async def reject_ungrounded(self: VerificationService, article: Article) -> list:
        raise UngroundedClaimError("LLM claim failed grounding")

    async def no_search(self: VerificationService, query: str) -> list[dict[str, str]]:
        return []

    monkeypatch.setattr(VerificationService, "_extract_claims", reject_ungrounded)
    monkeypatch.setattr(VerificationService, "_search", no_search)
    with factory() as db:
        article = _article()
        db.add(article)
        db.commit()
        run = asyncio.run(VerificationService(settings, db).verify(article))

        assert run.provider == "google_gemini"
        assert run.claims
        assert all(claim.text in article.content_excerpt for claim in run.claims)
        assert any("deterministic source-grounded" in warning for warning in run.warnings)


def test_groq_provider_keeps_claims_unconfirmed_without_web_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        llm_provider="groq",
        llm_api_key=SecretStr("test-groq-key"),
        llm_model="openai/gpt-oss-20b",
    )

    async def extract_from_source(self: VerificationService, article: Article) -> list:
        return VerificationService._extract_claims_deterministically(article)

    monkeypatch.setattr(VerificationService, "_extract_claims", extract_from_source)
    with factory() as db:
        article = _article()
        db.add(article)
        db.commit()
        run = asyncio.run(VerificationService(settings, db).verify(article))

        assert run.status == "UNCONFIRMED"
        assert run.provider == "groq"
        assert run.claims
        assert all(claim.status == "UNCONFIRMED" for claim in run.claims)
        assert any(
            "SEARXNG_BASE_URL, BRAVE_SEARCH_API_KEY, or TAVILY_API_KEY" in warning
            for warning in run.warnings
        )


def test_searxng_search_needs_no_api_key_and_filters_non_https_results() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="groq",
        llm_api_key=SecretStr("test-groq-key"),
        llm_model="openai/gpt-oss-20b",
        searxng_base_url="https://search.example/",
    )
    service = VerificationService(settings, None)

    with respx.mock:
        route = respx.get("https://search.example/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://research.example/article",
                            "title": "Research finding",
                        },
                        {
                            "url": "http://private.example/internal",
                            "title": "Non-HTTPS result",
                        },
                    ]
                },
            )
        )
        citations = asyncio.run(service._search("research claim"))

    assert citations == [
        {
            "url": "https://research.example/article",
            "title": "Research finding",
        }
    ]
    assert route.calls[0].request.url.params["format"] == "json"
    assert route.calls[0].request.url.params["q"] == "research claim"


def test_searxng_base_url_allows_http_only_on_loopback() -> None:
    settings = Settings(
        _env_file=None,
        searxng_base_url=" http://127.0.0.1:8888/ ",
    )
    assert settings.searxng_base_url == "http://127.0.0.1:8888"

    with pytest.raises(ValueError, match="HTTPS or HTTP on loopback"):
        Settings(_env_file=None, searxng_base_url="http://search.example")


def test_groq_verification_uses_brave_search_and_assesses_retrieved_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr("app.verification.service.validate_public_url", allow)
    monkeypatch.setattr("app.sources.manual.url_reader.validate_public_url", allow)
    article_claim = "The vendor released a tested AI model."
    source_quote = "The vendor released a tested AI model after internal evaluation."
    page = (
        "<html><head><title>Vendor release</title></head><body>"
        f"<article>{source_quote}</article></body></html>"
    )
    groq_responses = [
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "claims": [
                                        {
                                            "claim": article_claim,
                                            "search_query": "vendor tested AI model release",
                                            "is_material": True,
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        ),
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "stance": "SUPPORTS",
                                    "is_materially_misleading": False,
                                    "excerpt": source_quote,
                                    "rationale": "The page directly supports the model release.",
                                }
                            )
                        }
                    }
                ]
            },
        ),
    ]
    settings = Settings(
        _env_file=None,
        llm_provider="groq",
        llm_api_key=SecretStr("test-groq-key"),
        llm_model="openai/gpt-oss-20b",
        brave_search_api_key=SecretStr("test-brave-key"),
        max_evidence_per_claim=1,
    )

    with factory() as db, respx.mock:
        db.add(
            SourceCredibility(
                domain="vendor.example",
                tier="HIGH",
                rationale="Established primary vendor publication.",
                updated_by="test",
            )
        )
        article = _article()
        db.add(article)
        db.commit()
        groq = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            side_effect=groq_responses
        )
        brave = respx.get("https://api.search.brave.com/res/v1/web/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "url": "https://vendor.example/model",
                                "title": "Vendor release",
                                "description": source_quote,
                            },
                            {
                                "url": "http://127.0.0.1/private",
                                "title": "Unsafe result",
                            },
                        ]
                    }
                },
            )
        )
        respx.get("https://vendor.example/model").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=page,
            )
        )

        run = asyncio.run(VerificationService(settings, db).verify(article))

        assert run.provider == "groq"
        assert run.model_name == "openai/gpt-oss-20b"
        assert run.status == "SUPPORTED"
        assert len(run.claims[0].evidence) == 1
        assert run.claims[0].evidence[0].excerpt == source_quote
        assert groq.call_count == 2
        assert brave.call_count == 1
        brave_request = brave.calls[0].request
        assert brave_request.headers["x-subscription-token"] == "test-brave-key"
        assert brave_request.url.params["q"] == "vendor tested AI model release"
        evidence_urls = {item.url for item in run.claims[0].evidence}
        assert "http://127.0.0.1/private" not in evidence_urls


def test_v2_admin_endpoints_expose_verification_and_editable_credibility() -> None:
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
    )
    client = TestClient(create_app(settings, engine))
    with client:
        factory = client.app.state.session_factory
        with factory() as db:
            article = _article()
            db.add(article)
            db.commit()
            article_id = article.id

        unauthorized = client.post(f"/api/v1/articles/{article_id}/verify")
        assert unauthorized.status_code == 401
        headers = {"Authorization": "Bearer test-admin-token"}
        verified = client.post(
            f"/api/v1/articles/{article_id}/verify",
            headers=headers,
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["status"] == "UNCONFIRMED"
        assert verified.json()["claims"]
        loaded = client.get(
            f"/api/v1/articles/{article_id}/verification",
            headers=headers,
        )
        assert loaded.status_code == 200
        assert loaded.json()["id"] == verified.json()["id"]

        credibility = client.put(
            "/api/v1/sources/credibility/vendor.example",
            headers=headers,
            json={
                "tier": "AUTHORITATIVE",
                "rationale": "Official vendor advisory domain and original publication.",
            },
        )
        assert credibility.status_code == 200, credibility.text
        assert credibility.json()["updated_by"] == "admin"
        listed = client.get("/api/v1/sources/credibility", headers=headers)
        assert listed.status_code == 200
        assert listed.json()[0]["domain"] == "vendor.example"
