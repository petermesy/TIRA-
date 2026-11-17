from functools import lru_cache
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./tira.db"
    admin_api_token: SecretStr | None = None
    llm_provider: Literal["google_gemini", "groq", "openai_compatible"] = "google_gemini"
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    brave_search_api_key: SecretStr | None = None
    searxng_base_url: str = ""
    telegram_bot_token: SecretStr | None = None
    telegram_channel_id: str = ""
    telegram_review_chat_id: str = ""
    publish_mode: Literal["manual", "semi_automatic", "auto_with_gates"] = "manual"
    auto_publish_enabled: bool = False
    min_auto_importance_score: float = Field(default=8.5, ge=0.0, le=10.0)
    min_auto_verification_score: float = Field(default=0.95, ge=0.0, le=1.0)
    auto_publish_categories: list[str] = Field(default_factory=list)
    max_posts_per_day: int = Field(default=5, ge=1, le=100)
    telegram_retry_base_seconds: float = Field(default=5.0, ge=0, le=300)
    telegram_retry_max_seconds: float = Field(default=300.0, ge=0, le=3_600)
    telegram_max_attempts: int = Field(default=5, ge=1, le=10)
    telegram_reviewer_ids: list[str] = Field(default_factory=list)
    telegram_webhook_secret: SecretStr | None = None
    max_article_chars: int = Field(default=100_000, ge=1_000, le=1_000_000)
    max_fetch_bytes: int = Field(default=2_000_000, ge=1_024, le=20_000_000)
    max_telegram_message_length: int = Field(default=4_096, ge=256, le=4_096)
    max_requests_per_minute: int = Field(default=60, ge=1, le=10_000)
    max_collection_sources: int = Field(default=10, ge=1, le=100)
    max_source_requests_per_run: int = Field(default=30, ge=8, le=500)
    max_collection_items_per_run: int = Field(default=100, ge=1, le=1_000)
    max_llm_items_per_run: int = Field(default=10, ge=0, le=100)
    max_llm_items_per_day: int = Field(default=100, ge=0, le=10_000)
    collection_scheduler_enabled: bool = False
    collection_scheduler_interval_seconds: int = Field(default=300, ge=60, le=3_600)
    web_discovery_enabled: bool = False
    web_discovery_interval_seconds: int = Field(default=3_600, ge=900, le=86_400)
    max_web_discovery_items_per_run: int = Field(default=5, ge=1, le=20)
    max_collection_duration_seconds: int = Field(default=120, ge=1, le=900)
    max_article_age_days: int = Field(default=30, ge=1, le=3650)
    max_verification_claims: int = Field(default=8, ge=1, le=20)
    max_evidence_per_claim: int = Field(default=3, ge=1, le=10)
    max_verification_requests: int = Field(default=80, ge=1, le=200)
    max_verification_duration_seconds: int = Field(default=120, ge=1, le=900)
    importance_weights: dict[str, dict[str, float]] = Field(default_factory=dict)
    min_importance_score: float = Field(default=6.0, ge=0.0, le=10.0)
    min_verification_score: float = Field(default=0.8, ge=0.0, le=1.0)
    http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("llm_base_url")
    @classmethod
    def strip_llm_base_url_suffix(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("searxng_base_url")
    @classmethod
    def normalize_searxng_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized or normalized.startswith("https://"):
            return normalized

        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        is_loopback = hostname == "localhost"
        if hostname and not is_loopback:
            try:
                is_loopback = ip_address(hostname).is_loopback
            except ValueError:
                is_loopback = False

        if (
            parsed.scheme != "http"
            or not is_loopback
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("SEARXNG_BASE_URL must use HTTPS or HTTP on loopback")
        return normalized

    @property
    def llm_api_base_url(self) -> str:
        if (
            self.llm_provider == "groq"
            and self.llm_base_url == "https://generativelanguage.googleapis.com/v1beta"
        ):
            return "https://api.groq.com/openai/v1"
        return self.llm_base_url

    @property
    def tavily_search_configured(self) -> bool:
        return bool(self.tavily_api_key and self.tavily_api_key.get_secret_value().strip())

    @property
    def brave_search_configured(self) -> bool:
        return bool(
            self.brave_search_api_key and self.brave_search_api_key.get_secret_value().strip()
        )

    @property
    def searxng_search_configured(self) -> bool:
        return bool(self.searxng_base_url)

    @model_validator(mode="after")
    def validate_importance_weights(self) -> "Settings":
        dimensions = {
            "relevance",
            "novelty",
            "impact",
            "credibility",
            "technical_significance",
        }
        for category, weights in self.importance_weights.items():
            allowed_categories = {
                "AI",
                "CYBERSECURITY",
                "SOFTWARE",
                "EMERGING_TECH",
                "SCIENCE",
                "OTHER",
            }
            if category not in allowed_categories:
                raise ValueError(f"Unknown category in importance_weights: {category}")
            if set(weights) != dimensions:
                raise ValueError(
                    f"importance_weights for {category} must define exactly: "
                    f"{', '.join(sorted(dimensions))}"
                )
            if any(not 0 <= weight <= 1 for weight in weights.values()):
                raise ValueError("importance_weights values must be between 0 and 1")
            if abs(sum(weights.values()) - 1.0) > 0.0001:
                raise ValueError(f"importance_weights for {category} must sum to 1")
        return self

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.llm_api_key
            and self.llm_api_key.get_secret_value().strip()
            and self.llm_model.strip()
        )

    @property
    def telegram_configured(self) -> bool:
        return bool(
            self.telegram_bot_token
            and self.telegram_bot_token.get_secret_value().strip()
            and self.telegram_channel_id.strip()
            and self.telegram_review_chat_id.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
