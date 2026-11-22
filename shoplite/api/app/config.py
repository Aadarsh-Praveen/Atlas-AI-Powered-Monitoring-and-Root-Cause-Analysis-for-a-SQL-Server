"""Settings for the ShopLite API, read from the environment and validated.

Values come in layers (tools/envconfig.py): real environment variables,
then shoplite/api/.env if present, then the repo's root .env. Locally the
root .env is enough (DB_PASSWORD falls back to SHOPLITE_APP_PASSWORD there);
on the VM the API's own .env is its only source (LOAD_ROOT_ENV=false; the
root .env is locked to Administrators anyway). Everything the API can be
configured with is a field on Settings; code never reads os.environ directly.

If a required value is missing or invalid, creating Settings raises an error
naming the field, and the API refuses to start. Failing at startup is far
easier to diagnose than failing on the first request.
"""

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The API's folder (its optional .env lives here) and the repo root, found
# relative to this file so the API can be started from any folder.
API_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = API_DIR.parent.parent

# The shared layered loader (tools/envconfig.py): fills the process
# environment from shoplite/api/.env, then the root .env, without replacing
# anything already set. Settings below then read the environment.
sys.path.insert(0, str(REPO_ROOT / "tools"))
from envconfig import load_layers  # noqa: E402

PLACEHOLDER = "replace-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # No env_file here: load_layers() (in get_settings) has already put the
        # files' values into the environment, in the right order.
        case_sensitive=False,   # DB_HOST in .env fills db_host here
        extra="ignore",         # unrelated variables in the environment are fine
        populate_by_name=True,
    )

    # --- App -----------------------------------------------------------------
    # Semantic version, e.g. 1.0.0. Appears in X-App-Version and the session tag.
    app_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Database ------------------------------------------------------------
    db_host: str = "localhost"
    db_port: int = Field(default=1433, ge=1, le=65535)
    db_name: str = "SalesDB"
    db_user: str = "shoplite_app"
    # Required: no default. SecretStr prints as '**********' if logged.
    # DB_PASSWORD (the API's own .env, e.g. on the VM) or, locally, the root
    # .env's canonical SHOPLITE_APP_PASSWORD.
    db_password: SecretStr = Field(validation_alias=AliasChoices("DB_PASSWORD", "SHOPLITE_APP_PASSWORD"))
    # Defaults to the secure choice. The local .env sets it to true because
    # the container's certificate is self-signed.
    # DB_TRUST_SERVER_CERTIFICATE (the API's own .env; the VM sets it explicitly), else
    # the root .env's SQL_TRUST_SERVER_CERTIFICATE (true locally: the container's
    # certificate is self-signed), else the secure default, false.
    db_trust_server_certificate: bool = Field(
        default=False, validation_alias=AliasChoices("DB_TRUST_SERVER_CERTIFICATE", "SQL_TRUST_SERVER_CERTIFICATE"))

    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_query_timeout_seconds: int = Field(default=30, ge=1, le=600)

    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_pool_max_overflow: int = Field(default=5, ge=0, le=100)
    db_pool_timeout_seconds: int = Field(default=10, ge=1, le=120)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)

    # --- Web -----------------------------------------------------------------
    # Comma-separated in .env; read as text and split by allowed_origin_list.
    # (pydantic-settings would expect JSON for a list field.)
    allowed_origins: str = "http://localhost:3000"
    # Required. Demo admin "authentication": a shared token, not real security.
    admin_token: SecretStr

    # --- Feature flags -------------------------------------------------------
    # Comma-separated names of flags to turn on. Checked against the known
    # flags in app/flags.py at startup.
    feature_flags: str = ""

    # --- Simulated payment provider -------------------------------------------
    payment_decline_rate: float = Field(default=0.02, ge=0, le=1)
    payment_delay_min_ms: int = Field(default=200, ge=0, le=30000)
    payment_delay_max_ms: int = Field(default=800, ge=0, le=30000)

    # --- Simulated ranking service (flag search_ranking_v2, app/ranking.py) ----
    # Used only while the flag is on: how long each call takes, and how often
    # it fails. The incident injector (Phase 5) sets these.
    ranking_service_delay_ms: int = Field(default=2500, ge=0, le=30000)
    ranking_service_error_rate: float = Field(default=0.5, ge=0, le=1)

    # --- Telemetry (Phase 5, app/telemetry.py) ----------------------------------
    # Application Insights connection string. Not set = telemetry off (local).
    # Treated as a secret: whoever has it can send data into the resource.
    applicationinsights_connection_string: SecretStr | None = None
    # Share of requests kept (each with its SQL calls). 0.25 = one in four.
    telemetry_sampling_ratio: float = Field(default=0.25, gt=0, le=1)

    # --- Validation ----------------------------------------------------------

    @field_validator("db_password", "admin_token")
    @classmethod
    def reject_placeholder(cls, value: SecretStr, info) -> SecretStr:
        """Catch a .env copied from the example but never filled in."""
        if value.get_secret_value().strip() in ("", PLACEHOLDER):
            raise ValueError(f"{info.field_name.upper()} still has the placeholder value; set it in .env")
        return value

    @field_validator("applicationinsights_connection_string")
    @classmethod
    def connection_string_not_placeholder(cls, value: SecretStr | None) -> SecretStr | None:
        """Empty means telemetry off; the template's placeholder means someone forgot to fill it in."""
        if value is not None and value.get_secret_value().strip() == PLACEHOLDER:
            raise ValueError("APPLICATIONINSIGHTS_CONNECTION_STRING still has the placeholder value: "
                             "set it, or leave it empty to turn telemetry off")
        return value

    @field_validator("admin_token")
    @classmethod
    def admin_token_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 16:
            raise ValueError("ADMIN_TOKEN must be at least 16 characters")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def origins_look_valid(cls, value: str) -> str:
        """Each origin must be scheme://host[:port] exactly as the browser sends it."""
        for origin in split_list(value):
            if origin == "*":
                # A wildcard would let any website call the API from a visitor's browser.
                raise ValueError("ALLOWED_ORIGINS must list origins explicitly, not '*'")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"origin {origin!r} must start with http:// or https://")
            if origin.endswith("/"):
                # Browsers send "http://localhost:3000", never with a trailing slash,
                # so "http://localhost:3000/" would never match.
                raise ValueError(f"origin {origin!r} must not end with '/'")
        return value

    @model_validator(mode="after")
    def payment_delay_range(self) -> "Settings":
        if self.payment_delay_min_ms > self.payment_delay_max_ms:
            raise ValueError("PAYMENT_DELAY_MIN_MS must not be greater than PAYMENT_DELAY_MAX_MS")
        return self

    # --- Convenience views ---------------------------------------------------

    @property
    def allowed_origin_list(self) -> list[str]:
        return split_list(self.allowed_origins)

    @property
    def enabled_flag_names(self) -> frozenset[str]:
        # Lowercased so "Slow_Report" and "slow_report" mean the same flag.
        return frozenset(name.lower() for name in split_list(self.feature_flags))

    @property
    def session_tag(self) -> str:
        """Program name SQL Server shows for the API's sessions."""
        return f"ShopLite-API/{self.app_version}"


def split_list(value: str) -> list[str]:
    """'a, b,,c ' -> ['a', 'b', 'c']"""
    return [part.strip() for part in value.split(",") if part.strip()]


@lru_cache
def get_settings() -> Settings:
    """Load settings once and reuse them (lru_cache makes this a singleton).

    Tests can call get_settings.cache_clear() to reload after changing the
    environment.
    """
    load_layers(API_DIR)        # shoplite/api/.env, then the root .env; real variables win
    return Settings()
