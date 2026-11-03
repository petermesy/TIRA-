import asyncio

import pytest
from app.core.security import canonicalize_url, validate_public_url
from app.intelligence.scoring.calculator import calculate_importance


def test_importance_score_uses_documented_weights() -> None:
    assert (
        calculate_importance(
            {
                "relevance": 9,
                "novelty": 8,
                "impact": 8.5,
                "credibility": 7,
                "technical_significance": 8,
            }
        )
        == 8.15
    )


def test_score_validation_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="0 to 10"):
        calculate_importance(
            {
                "relevance": 11,
                "novelty": 8,
                "impact": 8,
                "credibility": 7,
                "technical_significance": 8,
            }
        )


def test_canonicalize_url_removes_tracking_and_fragment() -> None:
    assert (
        canonicalize_url("HTTPS://Example.ORG:443/Research?utm_source=news&id=4#section")
        == "https://example.org/Research?id=4"
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:pass@example.org/",
    ],
)
def test_public_url_validation_rejects_unsafe_destinations(url: str) -> None:
    with pytest.raises(ValueError):
        asyncio.run(validate_public_url(url))
