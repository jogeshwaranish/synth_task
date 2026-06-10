"""Typed settings loaded from the environment / .env.

Single place secrets enter the process. Nothing here is ever logged or printed
(see `safe_summary()` for the redacted view). Owners: Basil.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Strava ---
    strava_client_id: str | None = None
    strava_client_secret: str | None = Field(default=None, repr=False)
    strava_athlete_id: str = "basil"
    strava_redirect_port: int = 8721
    strava_scope: str = "activity:read_all"

    # --- Anthropic ---
    anthropic_api_key: str | None = Field(default=None, repr=False)
    anthropic_model: str = "claude-opus-4-8"

    # --- Google Sheets (live source; fixture path needs none of this) ---
    google_credentials_json: str | None = None
    google_sheet_id: str | None = None

    # --- Storage ---
    synth_db_path: Path = Path("synth.db")
    synth_token_dir: Path = Path(".tokens")

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.strava_redirect_port}/callback"

    @property
    def strava_token_path(self) -> Path:
        return self.synth_token_dir / "strava_token.json"

    @property
    def encryption_key_path(self) -> Path:
        # Per-machine AES-256-GCM key, auto-generated 0600. Never committed.
        return self.synth_token_dir / "synth.key"

    def safe_summary(self) -> dict[str, object]:
        """Redacted view safe to log. Secrets reduced to set/unset booleans."""
        return {
            "strava_client_id": self.strava_client_id,
            "strava_client_secret_set": bool(self.strava_client_secret),
            "strava_athlete_id": self.strava_athlete_id,
            "redirect_uri": self.redirect_uri,
            "anthropic_api_key_set": bool(self.anthropic_api_key),
            "anthropic_model": self.anthropic_model,
            "google_sheet_id_set": bool(self.google_sheet_id),
            "db_path": str(self.synth_db_path),
            "token_dir": str(self.synth_token_dir),
            "encryption_key_path": str(self.encryption_key_path),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
