"""Configuration. Every tunable lives here so cost and sensitivity can be
adjusted without a code change."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Printer (LAN)
    bambu_host: str
    bambu_serial: str
    bambu_access_code: str
    bambu_mqtt_port: int = 8883
    bambu_camera_port: int = 6000

    # Vision
    anthropic_api_key: str
    vision_model: str = "claude-sonnet-5"
    vision_effort: str = "low"
    vision_max_tokens: int = 2048
    frame_upload_width: int = 640

    # Notifications
    discord_webhook_url: str
    printer_label: str = "P1S"

    # Detection policy
    normal_interval: int = 45
    suspicious_interval: int = 10
    suspicion_threshold: float = 0.80
    confirmation_threshold: float = 0.85
    suspicion_max_checks: int = 6
    alert_cooldown_minutes: int = 15
    frame_history: int = 3

    # Storage
    data_dir: Path = Path("data")
    save_frames: bool = True
    max_session_frames: int = 2000

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
