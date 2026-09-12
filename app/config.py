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
    notify_on_start: bool = True
    notify_on_finish: bool = True

    # Rates used only to display monitoring spend in the finish notification.
    # Update these if you change VISION_MODEL; defaults are claude-sonnet-5.
    vision_input_cost_per_mtok: float = 2.0
    vision_output_cost_per_mtok: float = 10.0

    # Detection policy
    normal_interval: int = 45
    suspicious_interval: int = 10
    suspicion_threshold: float = 0.80
    confirmation_threshold: float = 0.85
    suspicion_max_checks: int = 6
    alert_cooldown_minutes: int = 15
    frame_history: int = 3

    # Filament cost. Two settings rather than a per-gram constant so a
    # different spool size or price needs no code change.
    spool_cost: float = 13.0
    spool_weight_g: float = 1000.0

    # Sliced-file fetch over FTPS. Read-only, and never while printing.
    enable_slice_fetch: bool = True
    bambu_ftp_port: int = 990
    ftp_timeout_seconds: float = 30.0
    ftp_max_fetch_bytes: int = 33554432  # 32MB
    ftp_search_dirs: str = "/,/cache"

    # Storage
    store_final_frame: bool = True
    final_frame_width: int = 640
    data_dir: Path = Path("data")
    save_frames: bool = True
    max_session_frames: int = 2000

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bambu_watch.db"

    @property
    def cost_per_gram(self) -> float:
        if self.spool_weight_g <= 0:
            return 0.0
        return self.spool_cost / self.spool_weight_g

    @property
    def ftp_dirs(self) -> list[str]:
        return [d.strip() for d in self.ftp_search_dirs.split(",") if d.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
