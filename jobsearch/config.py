"""Application settings, loaded from environment variables / .env."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SubmitMode(str, Enum):
    """How far the agent goes once you approve an application."""

    dry_run = "dry_run"
    """Build the exact request that would be sent, record it, submit nothing."""

    live = "live"
    """Actually POST the application to the ATS."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage -------------------------------------------------------
    data_dir: Path = Field(default_factory=lambda: Path.cwd() / "data")
    """Where your CV, profile, database and generated packets live.

    Relative to the working directory, not to the installed package — an
    installed copy must never write into site-packages. Docker sets DATA_DIR=/data.
    """
    database_url: str = ""

    # --- Claude --------------------------------------------------------
    anthropic_api_key: str = ""
    model: str = "claude-opus-5"
    scoring_effort: str = "low"
    """Effort for the high-volume fit-scoring pass. Runs on every candidate job."""
    writing_effort: str = "high"
    """Effort for cover letters and statements of purpose. Runs only on matches."""

    # --- Dashboard -----------------------------------------------------
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_token: str = ""
    """Shared secret for the dashboard. Empty disables auth (fine on localhost)."""
    public_base_url: str = ""
    """Base URL used in emails. Defaults to http://<host>:<port>."""

    # --- Scheduling ----------------------------------------------------
    digest_cron: str = "0 8 * * *"
    """When the daily discover+digest run fires (5-field cron, local time)."""
    timezone: str = "UTC"
    run_on_startup: bool = False

    # --- Pipeline behaviour --------------------------------------------
    min_score: int = 70
    """Fit score (0-100) a job must reach to enter your review queue."""
    max_new_applications_per_day: int = 15
    """Cap on documents generated per run. Protects your API spend."""
    max_jobs_scored_per_run: int = 300
    submit_mode: SubmitMode = SubmitMode.dry_run
    auto_submit_on_approval: bool = True
    """When true, approving in the dashboard immediately triggers submission."""

    # --- HTTP ----------------------------------------------------------
    http_timeout: float = 30.0
    http_user_agent: str = (
        "Mozilla/5.0 (compatible; jobsearch-ai/0.1; +https://github.com/vurghunimran/Jobsearch-AI)"
    )
    source_concurrency: int = 6

    # --- Email ---------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_from: str = ""
    digest_to: str = ""

    # --- Optional source credentials -----------------------------------
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""

    @field_validator("scoring_effort", "writing_effort")
    @classmethod
    def _valid_effort(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "xhigh", "max"}
        if v not in allowed:
            raise ValueError(f"effort must be one of {sorted(allowed)}, got {v!r}")
        return v

    # --- Derived paths --------------------------------------------------
    @property
    def profile_path(self) -> Path:
        return self.data_dir / "profile.yaml"

    @property
    def cv_dir(self) -> Path:
        return self.data_dir / "cv"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'jobsearch.db'}"

    @property
    def resolved_base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        return f"http://{self.dashboard_host}:{self.dashboard_port}"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.cv_dir, self.output_dir, self.data_dir / "logs"):
            path.mkdir(parents=True, exist_ok=True)

    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.digest_to)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
