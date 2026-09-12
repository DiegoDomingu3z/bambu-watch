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


def test_build_repository_returns_a_repository(monkeypatch, tmp_path):
    from app.config import Settings
    from app.main import build_repository

    settings = Settings(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                        anthropic_api_key="k", discord_webhook_url="u",
                        data_dir=tmp_path)
    repo = build_repository(settings)
    assert repo is not None
    assert (tmp_path / "bambu_watch.db").exists()


def test_build_repository_survives_an_unusable_path(tmp_path):
    from app.config import Settings
    from app.main import build_repository

    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    settings = Settings(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                        anthropic_api_key="k", discord_webhook_url="u",
                        data_dir=blocker)
    assert build_repository(settings) is None, (
        "an unopenable database must not stop the service starting"
    )
