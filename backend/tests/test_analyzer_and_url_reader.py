import asyncio
import json

import httpx
import pytest
import respx
from app.core.config import Settings
from app.intelligence.analyzer.service import AnalysisInvalidResponse, AnalyzerService
from app.sources.manual.url_reader import ArticleExtractionError, extract_url
from pydantic import SecretStr


def _llm_output(
    *, relevance: float = 7, evidence: str = "The research team published a model."
) -> dict:
    return {
        "category": "AI",
        "headline": "Research team publishes a model",
        "summary": "The research team published a model.",
        "why_it_matters": None,
        "scores": {
            "relevance": relevance,
            "novelty": 6,
            "impact": 5,
            "credibility": 7,
            "technical_significance": 6,
        },
        "rationale": {
            "relevance": "The article discusses AI research.",
            "novelty": "No comparison corpus was provided.",
            "impact": "The text reports a release.",
            "credibility": "A source is identified.",
            "technical_significance": "The text describes a model.",
        },
        "evidence_references": {
            "relevance": [evidence],
            "novelty": [],
            "impact": [],
            "credibility": [],
            "technical_significance": [evidence],
        },
    }


def test_llm_output_with_invalid_score_is_rejected() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("test-key"),
        llm_model="test-model",
        llm_base_url="https://llm.example/v1",
    )
    payload = {"choices": [{"message": {"content": json.dumps(_llm_output(relevance=11))}}]}
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=payload)
        )
        with pytest.raises(AnalysisInvalidResponse, match="malformed or out-of-range"):
            asyncio.run(
                AnalyzerService(settings).analyze(
                    title="A research release",
                    text="The research team published a model.",
                    url=None,
                    category_hint=None,
                )
            )


def test_llm_cannot_cite_text_not_in_source() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_api_key=SecretStr("test-key"),
        llm_model="test-model",
        llm_base_url="https://llm.example/v1",
    )
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(_llm_output(evidence="The source says something else."))
                }
            }
        ]
    }
    with respx.mock:
        respx.post("https://llm.example/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=payload)
        )
        with pytest.raises(AnalysisInvalidResponse, match="not present"):
            asyncio.run(
                AnalyzerService(settings).analyze(
                    title="A research release",
                    text="The research team published a model.",
                    url=None,
                    category_hint=None,
                )
            )


def test_gemini_provider_uses_generate_content_contract() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="google_gemini",
        llm_api_key=SecretStr("test-key"),
        llm_model="gemini-3.5-flash",
    )
    response_payload = {
        "candidates": [{"content": {"parts": [{"text": json.dumps(_llm_output())}]}}]
    }
    with respx.mock:
        route = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.5-flash:generateContent"
        ).mock(return_value=httpx.Response(200, json=response_payload))
        result = asyncio.run(
            AnalyzerService(settings).analyze(
                title="A research release",
                text="The research team published a model.",
                url=None,
                category_hint=None,
            )
        )
    request = route.calls[0].request
    body = json.loads(request.content)
    assert request.headers["x-goog-api-key"] == "test-key"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["systemInstruction"]["parts"][0]["text"]
    assert result.provider == "google_gemini"
    assert result.model_name == "gemini-3.5-flash"


def test_url_reader_extracts_only_bounded_public_html(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow_test_host(url: str) -> str:
        return url

    monkeypatch.setattr("app.sources.manual.url_reader.validate_public_url", allow_test_host)
    settings = Settings(_env_file=None, max_fetch_bytes=10_000)
    html = (
        "<html><head><title>Research note</title>"
        '<meta name="author" content="TIRA Lab"></head>'
        "<body><nav>Navigation noise</nav><article>Researchers describe a new AI model.</article>"
        "<script>ignored()</script></body></html>"
    )
    with respx.mock:
        respx.get("https://public.example/article").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=html,
            )
        )
        article = asyncio.run(extract_url("https://public.example/article", settings))
    assert article.title == "Research note"
    assert article.author == "TIRA Lab"
    assert "Researchers describe a new AI model." in article.text
    assert "Navigation noise" not in article.text


def test_url_reader_revalidates_redirect_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reject_private_redirect(url: str) -> str:
        if "127.0.0.1" in url:
            raise ValueError("URL resolves to a non-public network address")
        return url

    monkeypatch.setattr(
        "app.sources.manual.url_reader.validate_public_url",
        reject_private_redirect,
    )
    with respx.mock:
        route = respx.get("https://public.example/article").mock(
            return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        )
        with pytest.raises(ArticleExtractionError, match="Redirect target is not allowed"):
            asyncio.run(
                extract_url(
                    "https://public.example/article",
                    Settings(_env_file=None),
                )
            )
    assert route.call_count == 1
