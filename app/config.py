"""Application settings, read from environment variables and validated at startup."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Empty values (e.g. `AI_API_KEY=` in .env) count as unset.
    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    public_base_url: str
    data_dir: Path = Path("/data")
    puid: int = 1000
    pgid: int = 1000

    household_password: SecretStr | None = None

    ai_provider: Literal["anthropic", "fake"] = "anthropic"
    ai_api_key: SecretStr | None = None
    ai_model: str = "claude-sonnet-5-5"
    fake_ai_script: Path | None = None
    ai_input_price_per_mtok: float | None = Field(default=None, ge=0)
    ai_output_price_per_mtok: float | None = Field(default=None, ge=0)
    monthly_ai_budget: float | None = Field(default=None, ge=0)
    tz: str = "UTC"
    ai_confidence_threshold: float = Field(default=0.7, ge=0, le=1)
    ai_timeout_seconds: float = Field(default=60, gt=0)
    ai_max_image_edge: int = Field(default=1500, gt=0)
    max_upload_mb: int = Field(default=25, gt=0)

    session_days: int = Field(default=30, gt=0)
    cookie_secure: Literal["auto", "true", "false"] = "auto"
    # Read natively by uvicorn (--proxy-headers); kept here so it is documented and visible.
    forwarded_allow_ips: str = "127.0.0.1,172.16.0.0/12"
    allowed_hosts: str | None = None

    worker_autostart: bool = True
    backup_interval_hours: float = Field(default=24, gt=0)

    @field_validator("public_base_url")
    @classmethod
    def _check_public_base_url(cls, value: str) -> str:
        value = value.strip()
        if value.endswith("/"):
            value = value[:-1]
        parts = urlsplit(value)
        try:
            parts.port  # noqa: B018 - raises ValueError on a malformed port
        except ValueError as exc:
            raise ValueError(f"PUBLIC_BASE_URL has an invalid port: {value!r}") from exc
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.path
            or parts.query
            or parts.fragment
            or value.endswith(("?", "#"))
        ):
            raise ValueError(
                "PUBLIC_BASE_URL must be scheme://host[:port] with scheme http or https "
                f"and no path, query or credentials (got {value!r})"
            )
        return value

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown TZ {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check_budget_prices(self) -> "Settings":
        if self.monthly_ai_budget is not None and (
            self.ai_input_price_per_mtok is None or self.ai_output_price_per_mtok is None
        ):
            raise ValueError(
                "MONTHLY_AI_BUDGET requires AI_INPUT_PRICE_PER_MTOK and AI_OUTPUT_PRICE_PER_MTOK"
            )
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "inventory.db"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip().lower() for h in (self.allowed_hosts or "").split(",") if h.strip()]
