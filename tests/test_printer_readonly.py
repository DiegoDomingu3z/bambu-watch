"""The printer is read-only. This is the operator's controlling requirement,
so it is enforced by scanning the source rather than by convention.

The scan matches FTP verbs in the shape they would actually appear -- as a
quoted protocol command, or as an unambiguous ftplib write method -- rather
than as bare substrings. A naive substring scan flags SQL `DELETE FROM` as
the FTP `DELE` command, which is a false positive that would block
legitimate local-database work while proving nothing about the printer.
"""

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
FTP_CLIENT = APP / "bambu" / "ftp_client.py"

# An FTP command as it appears in source: quoted, verb first.
FTP_WRITE_COMMAND = re.compile(
    r"""["'](?:STOR|APPE|DELE|MKD|RMD|RNFR|RNTO|SITE|ALLO)\b""",
)
# ftplib write helpers with no plausible non-FTP meaning.
FTPLIB_WRITE_METHOD = re.compile(r"\.(?:storbinary|storlines|mkd|rmd)\s*\(")
# Every FTP verb, so the vocabulary check matches real commands rather than
# any run of capitals. Without the closed list a log string like
# "FTPS connect failed" reads as a command and the check is useless.
FTP_VERBS = (
    "RETR", "STOR", "STOU", "APPE", "DELE", "RNFR", "RNTO", "MKD", "RMD",
    "LIST", "NLST", "MLSD", "MLST", "CWD", "CDUP", "PWD", "TYPE", "MODE",
    "STRU", "PORT", "PASV", "EPSV", "EPRT", "REST", "SIZE", "MDTM", "ABOR",
    "QUIT", "USER", "PASS", "ACCT", "SYST", "STAT", "HELP", "NOOP", "FEAT",
    "OPTS", "AUTH", "PBSZ", "PROT", "SITE", "ALLO", "SMNT", "REIN",
)
ANY_FTP_COMMAND = re.compile(
    r"""["'](""" + "|".join(FTP_VERBS) + r""")\b"""
)
# Verbs that cannot modify anything on the printer.
READ_ONLY_VERBS = {
    "RETR", "LIST", "NLST", "MLSD", "MLST", "PWD", "CWD", "CDUP", "TYPE",
    "MODE", "STRU", "PASV", "EPSV", "REST", "SIZE", "MDTM", "SYST", "STAT",
    "FEAT", "OPTS", "NOOP", "QUIT", "USER", "PASS", "AUTH", "PBSZ", "PROT",
}

FORBIDDEN_CONTROL = [
    "gcode_line", "gcode_file_line", '"pause"', "'pause'",
    '"resume"', "'resume'", '"stop"', "'stop'",
]


def sources() -> list[Path]:
    return sorted(APP.rglob("*.py"))


def test_sources_were_found():
    assert len(sources()) > 5, "the scan must actually be scanning something"


def test_no_ftp_write_command_strings():
    hits = []
    for path in sources():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FTP_WRITE_COMMAND.search(line):
                hits.append(f"{path.name}:{line_no}")
    assert not hits, f"FTP write commands must never appear in app/: {hits}"


def test_no_ftplib_write_methods():
    hits = []
    for path in sources():
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FTPLIB_WRITE_METHOD.search(line):
                hits.append(f"{path.name}:{line_no}")
    assert not hits, f"ftplib write helpers must never be called: {hits}"


def test_ftp_client_vocabulary_is_retr_only():
    """The tightest assertion available: whatever FTP commands the client
    knows how to say, RETR is the only one."""
    if not FTP_CLIENT.exists():
        return
    text = FTP_CLIENT.read_text(encoding="utf-8")
    commands = {m.group(1) for m in ANY_FTP_COMMAND.finditer(text)}
    assert commands <= READ_ONLY_VERBS, (
        f"the FTPS client may only issue read-only commands; found "
        f"{sorted(commands - READ_ONLY_VERBS)}"
    )
    assert commands <= {"RETR"}, (
        f"today the client should need RETR alone; found {sorted(commands)}. "
        f"If a read-only command was added deliberately, widen this assertion."
    )


def test_ftp_client_exposes_no_write_methods():
    if not FTP_CLIENT.exists():
        return
    text = FTP_CLIENT.read_text(encoding="utf-8")
    for verb in ("def store", "def upload", "def delete", "def remove",
                 "def rename", "def mkdir", "def put", "def write"):
        assert verb not in text, f"{verb} must not exist on the FTPS client"


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
