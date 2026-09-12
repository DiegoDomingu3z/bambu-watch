import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = {
    "BAMBU_HOST": "192.168.1.50",
    "BAMBU_SERIAL": "01P00A000000000",
    "BAMBU_ACCESS_CODE": "abcd1234",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/x",
}


def test_defaults_match_detection_policy(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.vision_model == "claude-sonnet-5"
    assert s.vision_effort == "low"
    assert s.frame_upload_width == 640
    assert s.normal_interval == 45
    assert s.suspicious_interval == 10
    assert s.suspicion_threshold == 0.80
    assert s.confirmation_threshold == 0.85
    assert s.suspicion_max_checks == 6
    assert s.alert_cooldown_minutes == 15
    assert s.frame_history == 3


def test_env_overrides_defaults(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("NORMAL_INTERVAL", "90")
    monkeypatch.setenv("VISION_MODEL", "claude-opus-5")
    s = Settings()
    assert s.normal_interval == 90
    assert s.vision_model == "claude-opus-5"


def test_missing_required_secret_raises(monkeypatch, tmp_path):
    for k in REQUIRED:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        Settings()


def test_sessions_dir_derives_from_data_dir(monkeypatch, tmp_path):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings(data_dir=tmp_path)
    assert s.sessions_dir == tmp_path / "sessions"


def test_cost_per_gram_derives_from_spool(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings()
    assert s.spool_cost == 13.0
    assert s.spool_weight_g == 1000.0
    assert s.cost_per_gram == pytest.approx(0.013)
    assert 340 * s.cost_per_gram == pytest.approx(4.42)


def test_cost_per_gram_follows_a_different_spool(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SPOOL_COST", "24.99")
    monkeypatch.setenv("SPOOL_WEIGHT_G", "750")
    assert Settings().cost_per_gram == pytest.approx(0.03332)


def test_cost_per_gram_survives_zero_weight(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SPOOL_WEIGHT_G", "0")
    assert Settings().cost_per_gram == 0.0, "must not raise ZeroDivisionError"


def test_db_path_and_ftp_defaults(monkeypatch, tmp_path):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings(data_dir=tmp_path)
    assert s.db_path == tmp_path / "bambu_watch.db"
    assert s.enable_slice_fetch is True
    assert s.bambu_ftp_port == 990
    assert s.ftp_timeout_seconds == 30.0
    assert s.ftp_max_fetch_bytes == 33554432
    assert s.ftp_dirs == ["/", "/cache"]


def test_ftp_dirs_ignores_blank_entries(monkeypatch):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FTP_SEARCH_DIRS", "/, ,/cache, ")
    assert Settings().ftp_dirs == ["/", "/cache"]
