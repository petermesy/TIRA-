from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./tira.db"
    admin_api_token: SecretStr | None = None
    llm_provider: Literal["google_gemini", "openai_compatible"] = "google_gemini"
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_channel_id: str = ""
    telegram_review_chat_id: str = ""
    publish_mode: Literal["manual"] = "manual"
    max_article_chars: int = Field(default=100_000, ge=1_000, le=1_000_000)
    max_fetch_bytes: int = Field(default=2_000_000, ge=1_024, le=20_000_000)
    max_telegram_message_length: int = Field(default=4_096, ge=256, le=4_096)
    max_requests_per_minute: int = Field(default=60, ge=1, le=10_000)
    max_collection_sources: int = Field(default=10, ge=1, le=100)
    max_source_requests_per_run: int = Field(default=30, ge=8, le=500)
    max_collection_items_per_run: int = Field(default=100, ge=1, le=1_000)
    max_llm_items_per_run: int = Field(default=10, ge=0, le=100)
    max_llm_items_per_day: int = Field(default=100, ge=0, le=10_000)
    max_collection_duration_seconds: int = Field(default=120, ge=1, le=900)
    max_article_age_days: int = Field(default=30, ge=1, le=3650)
    min_importance_score: float = Field(default=6.0, ge=0.0, le=10.0)
    min_verification_score: float = Field(default=0.8, ge=0.0, le=1.0)
    http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("llm_base_url")
    @classmethod
    def strip_llm_base_url_suffix(cls, value: str) -> str:
        return value.rstrip("/")

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
