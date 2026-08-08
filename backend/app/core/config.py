"""Application settings.

Every value comes from the environment. Nothing is hardcoded and no secret has
a usable default — the validators below refuse to start with placeholder values
in production rather than silently running on a guessable key.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]
BillingMode = Literal["pay_per_use", "legacy_subscription"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Core ---------------------------------------------------------------
    environment: Environment = "development"
    log_level: str = "INFO"
    app_timezone: str = "UTC"

    # --- Security -----------------------------------------------------------
    secret_key: str
    token_encryption_key: str
    session_ttl_hours: int = 24
    cookie_secure: bool = False
    cookie_domain: str | None = None

    # --- Database -----------------------------------------------------------
    postgres_user: str = "xagent"
    postgres_password: str = ""
    postgres_db: str = "xagent"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # Set directly in tests to point at SQLite; otherwise assembled from the
    # POSTGRES_* parts.
    database_url_override: str | None = None

    # --- Redis --------------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"

    # --- CORS ---------------------------------------------------------------
    frontend_origin: str = "http://localhost:3000"

    # --- X API (Phase 3+) ---------------------------------------------------
    x_client_id: str = ""
    x_client_secret: str = ""
    x_redirect_uri: str = "http://localhost:8000/api/v1/x/oauth/callback"
    x_billing_mode: BillingMode = "pay_per_use"
    x_monthly_budget_usd: float = 25.00

    # Decision 4 safeguard: when false we never request `tweet.write`, so the
    # agent cannot publish even if every other control fails.
    x_enable_write_actions: bool = False

    # --- Anthropic (Phase 6+) ----------------------------------------------
    anthropic_api_key: str = ""
    anthropic_model_deep: str = "claude-opus-5"
    anthropic_model_fast: str = "claude-sonnet-5"

    # --- Rate limiting ------------------------------------------------------
    login_attempts_per_15min: int = Field(default=10, ge=1)
    api_requests_per_minute: int = Field(default=120, ge=1)

    # ------------------------------------------------------------------ derived
    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def encryption_key_bytes(self) -> bytes:
        return base64.urlsafe_b64decode(self.token_encryption_key)

    # ---------------------------------------------------------------- validators
    @field_validator("token_encryption_key")
    @classmethod
    def _validate_encryption_key(cls, v: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "TOKEN_ENCRYPTION_KEY must be base64url-encoded. Generate with: "
                'python -c "import base64,os; '
                'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
            ) from exc
        if len(raw) != 32:
            raise ValueError(
                f"TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes for AES-256-GCM, "
                f"got {len(raw)}."
            )
        return v

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters.")
        return v

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        """Refuse to boot production with development leftovers."""
        if not self.is_production:
            return self
        problems: list[str] = []
        if "CHANGE_ME" in self.secret_key or "CHANGE_ME" in self.token_encryption_key:
            problems.append("placeholder secrets are still in place")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true in production")
        if self.frontend_origin.startswith("http://"):
            problems.append("FRONTEND_ORIGIN must use HTTPS in production")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
