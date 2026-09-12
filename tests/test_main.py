import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import describe_config_error


def test_describe_config_error_lists_missing_variables(monkeypatch, tmp_path):
    for key in ("BAMBU_HOST", "BAMBU_SERIAL", "BAMBU_ACCESS_CODE",
                "ANTHROPIC_API_KEY", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as caught:
        Settings()

    text = describe_config_error(caught.value)
    assert "BAMBU_HOST" in text
    assert "BAMBU_ACCESS_CODE" in text
    assert "ANTHROPIC_API_KEY" in text
    assert "DISCORD_WEBHOOK_URL" in text
    assert ".env" in text, "must tell the operator how to fix it"
    assert "Traceback" not in text


def test_describe_config_error_reports_bad_values(monkeypatch, tmp_path):
    env = {
        "BAMBU_HOST": "h", "BAMBU_SERIAL": "s", "BAMBU_ACCESS_CODE": "c",
        "ANTHROPIC_API_KEY": "k", "DISCORD_WEBHOOK_URL": "u",
        "NORMAL_INTERVAL": "not-a-number",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValidationError) as caught:
        Settings()

    text = describe_config_error(caught.value)
    assert "normal_interval" in text
    assert ".env" in text
