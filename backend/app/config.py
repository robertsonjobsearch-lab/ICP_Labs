from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(value: str) -> str:
    # Render supplies a standard PostgreSQL URL; this app installs psycopg 3.
    for prefix in ("postgres://", "postgresql://"):
        if value.startswith(prefix):
            return "postgresql+psycopg://" + value[len(prefix):]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    app_env: Literal["development", "production"] = "development"
    database_url: str

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        return normalize_database_url(value)
    # Optional for the original stack; required by the Sites backend overlay.
    sites_proxy_secret: SecretStr | None = None

    @field_validator("sites_proxy_secret")
    @classmethod
    def _validate_proxy_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            secret = value.get_secret_value()
            if len(secret) < 32 or "\r" in secret or "\n" in secret:
                raise ValueError("SITES_PROXY_SECRET must contain at least 32 characters and no newlines")
        return value

    # Browser origins allowed to call the API. Also used as the trusted-origin
    # list for state-changing requests (CSRF defense).
    cors_allowed_origins: str = ""

    # When false, nobody signs in: every visitor shares one built-in workspace user.
    auth_required: bool = False

    session_ttl_hours: int = Field(default=12, ge=1, le=168)
    cookie_secure: bool = False

    # Must be set explicitly in production. Defaults to "mock" in development.
    contact_provider: str | None = None
    apollo_api_key: SecretStr | None = None

    login_rate_limit_per_minute: int = Field(default=5, ge=1, le=1000)
    search_rate_limit_per_minute: int = Field(default=20, ge=1, le=1000)

    @field_validator("contact_provider", mode="before")
    @classmethod
    def _blank_provider_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def resolved_contact_provider(self) -> str:
        return self.contact_provider or "mock"

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        origins = self.cors_origin_list
        if self.is_production:
            problems = []
            if "*" in origins:
                problems.append("CORS_ALLOWED_ORIGINS must not contain '*'")
            if not origins:
                problems.append("CORS_ALLOWED_ORIGINS must list the public site origin")
            if any(not o.startswith("https://") for o in origins):
                problems.append("CORS_ALLOWED_ORIGINS must use https:// origins")
            if not self.cookie_secure:
                problems.append("COOKIE_SECURE must be true")
            if not self.contact_provider:
                problems.append("CONTACT_PROVIDER must be set explicitly")
            if problems:
                raise ValueError("Invalid production configuration: " + "; ".join(problems))
        elif "*" in origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*'; list explicit origins")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

