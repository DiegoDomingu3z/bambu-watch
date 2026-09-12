import pytest

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
    with pytest.raises(Exception):
        Settings()


def test_sessions_dir_derives_from_data_dir(monkeypatch, tmp_path):
    for k, v in REQUIRED.items():
        monkeypatch.setenv(k, v)
    s = Settings(data_dir=tmp_path)
    assert s.sessions_dir == tmp_path / "sessions"
