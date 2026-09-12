"""The printer is read-only. This is the operator's controlling requirement,
so it is enforced by scanning the source rather than by convention."""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

FORBIDDEN_FTP = [
    "STOR", "APPE", "DELE", "MKD", "RMD", "RNFR", "RNTO", "SITE",
    "storbinary", "storlines", "storelines",
]
FORBIDDEN_CONTROL = [
    "gcode_line", "gcode_file_line", '"pause"', "'pause'",
    '"resume"', "'resume'", '"stop"', "'stop'",
]


def sources() -> list[Path]:
    return sorted(APP.rglob("*.py"))


def test_sources_were_found():
    assert len(sources()) > 5, "the scan must actually be scanning something"


def test_no_ftp_write_verbs_in_source():
    hits = []
    for path in sources():
        text = path.read_text(encoding="utf-8")
        for verb in FORBIDDEN_FTP:
            if verb in text:
                hits.append(f"{path.name}: {verb}")
    assert not hits, f"FTP write verbs must never appear in app/: {hits}"


def test_no_printer_control_commands_in_source():
    hits = []
    for path in sources():
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_CONTROL:
            if token in text:
                hits.append(f"{path.name}: {token}")
    assert not hits, f"printer control commands must never appear: {hits}"


def test_only_pushall_is_ever_published():
    publishing = [p for p in sources() if ".publish(" in p.read_text(encoding="utf-8")]
    assert [p.name for p in publishing] == ["mqtt_client.py"], (
        "only the MQTT client may publish to the printer"
    )

    text = publishing[0].read_text(encoding="utf-8")
    commands = set(re.findall(r'"command":\s*"([a-z_]+)"', text))
    assert commands == {"pushall"}, (
        f"pushall is the only permitted command; found {commands}"
    )


def test_ftp_client_exposes_no_write_methods():
    """Once the FTPS client exists it must expose retrieval only."""
    path = APP / "bambu" / "ftp_client.py"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    for verb in ("def store", "def upload", "def delete", "def remove",
                 "def rename", "def mkdir"):
        assert verb not in text, f"{verb} must not exist on the FTPS client"
