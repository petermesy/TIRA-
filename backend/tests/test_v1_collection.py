import asyncio
from datetime import UTC, datetime

import httpx
import pytest
import respx
from app.collection.service import execute_collection
from app.core.config import Settings
from app.database.base import Base
from app.database.models import Article, CollectionRun, Source
from app.main import create_app
from app.sources.connectors.arxiv import ArxivConnector
from app.sources.connectors.github_releases import GitHubReleasesConnector
from app.sources.connectors.rss import RSSConnector
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _session_factory() -> sessionmaker:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _api_client() -> TestClient:
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
    return TestClient(create_app(settings, engine))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, app_env="test")


def _allow_mock_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr("app.sources.connectors.http.validate_public_url", allow)
    monkeypatch.setattr("app.sources.connectors.rss.validate_public_url", allow)


def test_rss_connector_normalizes_items(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_mock_urls(monkeypatch)
    source = Source(
        id="rss-1",
        name="Research feed",
        source_type="RSS",
        base_url="https://feed.example/rss",
        domain="feed.example",
        enabled=True,
        categories=["AI"],
        max_items_per_run=10,
        interval_minutes=60,
        rate_limit_per_minute=600,
        consecutive_failures=0,
    )
    xml = """<?xml version="1.0"?>
    <rss><channel><title>Research Lab</title><item>
      <title>New AI research model release</title>
      <link>https://news.example/research?utm_source=feed</link>
      <enclosure url="https://images.example/research.jpg" type="image/jpeg" />
      <description><![CDATA[The research team published a new AI model with detailed technical
      notes.]]></description>
      <pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>"""
    with respx.mock:
        respx.get("https://feed.example/rss").mock(
            return_value=httpx.Response(
                200, text=xml, headers={"content-type": "application/rss+xml"}
            )
        )
        batch = asyncio.run(RSSConnector(settings).fetch(source, None, 10))
    assert len(batch.items) == 1
    assert batch.items[0].source_name == "Research Lab"
    assert str(batch.items[0].url) == "https://news.example/research"
    assert str(batch.items[0].image_url) == "https://images.example/research.jpg"
    assert batch.items[0].retrieval_method == "RSS"


def test_arxiv_connector_normalizes_official_atom_records(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_mock_urls(monkeypatch)
    source = Source(
        id="arxiv-1",
        name="arXiv AI",
        source_type="ARXIV",
        base_url="https://export.arxiv.org/api/query?search_query=cat:cs.AI",
        domain="export.arxiv.org",
        enabled=True,
        categories=["AI"],
        max_items_per_run=10,
        interval_minutes=60,
        rate_limit_per_minute=600,
        consecutive_failures=0,
    )
    xml = """<feed xmlns="http://www.w3.org/2005/Atom">
    <entry><id>http://arxiv.org/abs/1234.56789</id>
      <title>New AI model for research</title>
      <summary>This paper describes a detailed machine learning model and its evaluation.</summary>
      <published>2026-11-02T10:00:00Z</published>
      <author><name>Researcher</name></author>
    </entry></feed>"""
    with respx.mock:
        respx.get(source.base_url).mock(return_value=httpx.Response(200, text=xml))
        batch = asyncio.run(ArxivConnector(settings).fetch(source, None, 10))
    assert len(batch.items) == 1
    assert str(batch.items[0].url) == "https://arxiv.org/abs/1234.56789"
    assert batch.items[0].author == "Researcher"
    assert batch.items[0].retrieval_method == "API"


def test_github_connector_normalizes_public_releases(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_mock_urls(monkeypatch)
    source = Source(
        id="github-1",
        name="Example project releases",
        source_type="GITHUB_RELEASES",
        base_url="https://api.github.com/repos/example/project/releases",
        domain="api.github.com",
        enabled=True,
        categories=["SOFTWARE"],
        max_items_per_run=10,
        interval_minutes=60,
        rate_limit_per_minute=600,
        consecutive_failures=0,
    )
    data = [
        {
            "name": "Version 2 release",
            "tag_name": "v2.0",
            "html_url": "https://github.com/example/project/releases/tag/v2.0",
            "body": (
                "This release improves the open source developer framework with detailed notes."
            ),
            "published_at": "2026-11-02T10:00:00Z",
        }
    ]
    with respx.mock:
        respx.get(source.base_url).mock(return_value=httpx.Response(200, json=data))
        batch = asyncio.run(GitHubReleasesConnector(settings).fetch(source, None, 10))
    assert len(batch.items) == 1
    assert batch.items[0].title == "Version 2 release"
    assert batch.items[0].source_domain == "github.com"
    assert batch.items[0].retrieval_method == "API"


def test_collection_is_idempotent_and_records_run_metrics(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_mock_urls(monkeypatch)
    factory = _session_factory()
    xml = """<rss><channel><title>Research Lab</title><item>
      <title>AI research model announcement</title>
      <link>https://news.example/ai-model</link>
      <description><![CDATA[The research team announced an AI model and published technical notes
      for developers.]]></description>
      <pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>"""
    with factory() as db:
        db.add(
            Source(
                name="Research feed",
                source_type="RSS",
                base_url="https://feed.example/rss",
                domain="feed.example",
                categories=["AI"],
                enabled=True,
                interval_minutes=60,
                rate_limit_per_minute=600,
                max_items_per_run=10,
            )
        )
        db.commit()
        with respx.mock:
            respx.get("https://feed.example/rss").mock(return_value=httpx.Response(200, text=xml))
            first = asyncio.run(execute_collection(db, settings))
            second = asyncio.run(execute_collection(db, settings))
        assert first.status == "COMPLETED"
        assert first.items_found == 1
        assert first.items_new == 1
        assert first.items_analyzed == 1
        assert second.duplicates == 1
        assert second.items_new == 0
        assert len(db.scalars(select(Article)).all()) == 1
        assert len(db.scalars(select(CollectionRun)).all()) == 2
        assert first.source_metrics[0]["status"] == "COMPLETED"


def test_failed_source_does_not_stop_healthy_source(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_mock_urls(monkeypatch)
    factory = _session_factory()
    xml = """<rss><channel><item>
      <title>AI research model announcement</title>
      <link>https://news.example/ai-model</link>
      <description><![CDATA[The research team announced an AI model and published technical notes
      for developers.]]></description>
    </item></channel></rss>"""
    with factory() as db:
        db.add_all(
            [
                Source(
                    name="Broken GitHub source",
                    source_type="GITHUB_RELEASES",
                    base_url="https://api.github.com/repos/owner/repo",
                    domain="api.github.com",
                    categories=["AI"],
                    enabled=True,
                    interval_minutes=60,
                    rate_limit_per_minute=600,
                    max_items_per_run=5,
                ),
                Source(
                    name="Healthy feed",
                    source_type="RSS",
                    base_url="https://feed.example/rss",
                    domain="feed.example",
                    categories=["AI"],
                    enabled=True,
                    interval_minutes=60,
                    rate_limit_per_minute=600,
                    max_items_per_run=5,
                ),
            ]
        )
        db.commit()
        with respx.mock:
            respx.get("https://feed.example/rss").mock(return_value=httpx.Response(200, text=xml))
            run = asyncio.run(execute_collection(db, settings))
        assert run.status == "PARTIAL"
        assert run.sources_checked == 2
        assert run.items_new == 1
        assert any(error["error_class"] == "InvalidSource" for error in run.errors)
        assert db.scalar(select(Article)) is not None


def test_scheduled_run_skips_sources_before_poll_interval(settings: Settings) -> None:
    factory = _session_factory()
    with factory() as db:
        db.add(
            Source(
                name="Recently checked feed",
                source_type="RSS",
                base_url="https://feed.example/rss",
                domain="feed.example",
                categories=["AI"],
                enabled=True,
                interval_minutes=60,
                rate_limit_per_minute=600,
                max_items_per_run=5,
                last_checked_at=datetime.now(UTC),
            )
        )
        db.commit()
        run = asyncio.run(execute_collection(db, settings, trigger_type="SCHEDULED"))
    assert run.status == "COMPLETED"
    assert run.sources_checked == 0


def test_source_admin_endpoints_require_admin_and_toggle_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def allow(url: str) -> str:
        return url

    monkeypatch.setattr("app.api.routes.collection.validate_public_url", allow)
    client = _api_client()
    headers = {"Authorization": "Bearer test-admin-token"}
    payload = {
        "name": "Example AI feed",
        "source_type": "RSS",
        "base_url": "https://feed.example/rss",
        "categories": ["AI"],
        "interval_minutes": 60,
        "rate_limit_per_minute": 600,
        "max_items_per_run": 10,
    }
    with client:
        unauthorized = client.post("/api/v1/sources", json=payload)
        assert unauthorized.status_code == 401
        registered = client.post("/api/v1/sources", headers=headers, json=payload)
        assert registered.status_code == 201, registered.text
        source_id = registered.json()["id"]
        disabled = client.patch(
            f"/api/v1/sources/{source_id}",
            headers=headers,
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        listed = client.get("/api/v1/sources", headers=headers)
        assert listed.status_code == 200
        assert listed.json()[0]["id"] == source_id


def test_source_creation_rejects_non_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject(url: str) -> str:
        raise ValueError("URL resolves to a non-public network address")

    monkeypatch.setattr("app.api.routes.collection.validate_public_url", reject)
    client = _api_client()
    with client:
        response = client.post(
            "/api/v1/sources",
            headers={"Authorization": "Bearer test-admin-token"},
            json={
                "name": "Unsafe feed",
                "source_type": "RSS",
                "base_url": "https://127.0.0.1/feed",
                "categories": ["AI"],
            },
        )
    assert response.status_code == 422
