"""Test isolation.

Settings reads a `.env` file by default. Without this, every test that
constructs Settings inherits whatever the developer happens to have
configured locally, so the suite passes or fails depending on a file that is
not in the repository. That bug hid until a local NORMAL_INTERVAL diverged
from the default.

`isolated_env` removes the variables Settings reads and moves the working
directory away from the repository, so nothing on this machine can reach a
test. Tests that want a specific value pass it explicitly.
"""

import pytest

SETTINGS_ENV_VARS = (
    "BAMBU_HOST", "BAMBU_SERIAL", "BAMBU_ACCESS_CODE", "BAMBU_MQTT_PORT",
    "BAMBU_CAMERA_PORT", "BAMBU_FTP_PORT", "ANTHROPIC_API_KEY", "VISION_MODEL",
    "VISION_EFFORT", "VISION_MAX_TOKENS", "FRAME_UPLOAD_WIDTH",
    "DISCORD_WEBHOOK_URL", "PRINTER_LABEL", "NOTIFY_ON_START",
    "NOTIFY_ON_FINISH", "VISION_INPUT_COST_PER_MTOK",
    "VISION_OUTPUT_COST_PER_MTOK", "NORMAL_INTERVAL", "SUSPICIOUS_INTERVAL",
    "SUSPICION_THRESHOLD", "CONFIRMATION_THRESHOLD", "SUSPICION_MAX_CHECKS",
    "ALERT_COOLDOWN_MINUTES", "FRAME_HISTORY", "SPOOL_COST", "SPOOL_WEIGHT_G",
    "ENABLE_SLICE_FETCH", "FTP_TIMEOUT_SECONDS", "FTP_MAX_FETCH_BYTES",
    "FTP_SEARCH_DIRS", "DATA_DIR", "SAVE_FRAMES", "MAX_SESSION_FRAMES",
    "STORE_FINAL_FRAME", "FINAL_FRAME_WIDTH",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path_factory):
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Away from the repository, so the real .env is not discovered.
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
