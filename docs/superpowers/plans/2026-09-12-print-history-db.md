# Print History Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every print in a local SQLite database with name, dates, outcome, filament weight, length, colours and cost, fetching material figures from the printer's sliced file over a strictly read-only FTPS path.

**Architecture:** A new `app/storage/database.py` owns the SQLite connection and schema; `app/storage/prints.py` owns row lifecycle; `app/bambu/ftp_client.py` fetches and parses `Metadata/slice_info.config` out of the 3MF; `app/bambu/ams.py` normalizes the MQTT AMS block. `PrintMonitor` gains a fetch lifecycle bound to printer state and writes the row at print start and close. Outcome and consumption are pure functions.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `ftplib`, `zipfile`, `xml.etree.ElementTree`. No new third-party dependencies.

**Spec:** `docs/superpowers/specs/2026-09-12-print-history-db-design.md`

## Global Constraints

- **The printer is read-only. This overrides every other goal, including accuracy.**
- No FTP write verb may exist anywhere in `app/`: `STOR`, `APPE`, `DELE`, `MKD`, `RMD`, `RNFR`, `RNTO`, `SITE`, or the `ftplib` wrappers `storbinary`, `storlines`, `delete`, `mkd`, `rmd`, `rename`.
- The only MQTT publish in the codebase remains `pushall`.
- No FTPS read may be attempted while the printer reports `RUNNING`.
- One fetch per print, cached. No polling, no retry loop.
- `ENABLE_SLICE_FETCH=false` disables all FTPS access.
- Every FTPS and database failure is non-fatal and must not disturb the monitoring pipeline.
- Cost is `SPOOL_COST / SPOOL_WEIGHT_G`; defaults 13.0 and 1000.0, giving 0.013 per gram. Never hardcode 0.013.
- Consumption for a stopped print is an estimate. It must be labelled `layer_fraction` in the database and carry the word "estimated" wherever it is shown.
- Rows are inserted at print start with `outcome = 'running'` and updated at close.
- No new third-party dependency.
- All source ASCII. `uv pip install -e ".[dev]"` already installed.
- No test may require printer hardware, network access, or API credit.

---

## File Structure

| Path | Responsibility | Status |
|---|---|---|
| `app/config.py` | Add cost, FTPS and db settings | modify |
| `tests/test_printer_readonly.py` | Source scan enforcing the constraint | create |
| `app/storage/database.py` | Connection, pragmas, schema, migration | create |
| `app/storage/records.py` | `determine_outcome`, `compute_consumption` (pure) | create |
| `app/storage/prints.py` | `PrintRepository` row lifecycle | create |
| `app/bambu/slice_info.py` | `SliceInfo`, 3MF and XML parsing (no I/O) | create |
| `app/bambu/ftp_client.py` | Read-only implicit-TLS FTPS fetch | create |
| `app/bambu/ams.py` | MQTT AMS block to slots | create |
| `app/detection/monitor.py` | Fetch lifecycle, db writes | modify |
| `app/notifications/discord.py` | Material and cost lines | modify |
| `app/main.py` | Wire db and ftp client | modify |
| `scripts/probe_printer.py` | FTPS probe for the eight unknowns | modify |
| `README.md` | Database, cost, query examples | modify |

Parsing is split from fetching deliberately: `slice_info.py` is pure and fully
testable with a synthetic 3MF, so the only untestable-without-hardware part is
the socket work in `ftp_client.py`.

---

### Task 1: Configuration and the read-only constraint test

The constraint test comes first so it guards every task after it.

**Files:**
- Modify: `app/config.py`
- Test: `tests/test_config.py` (extend), `tests/test_printer_readonly.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` gains `spool_cost: float = 13.0`, `spool_weight_g: float = 1000.0`,
  `enable_slice_fetch: bool = True`, `bambu_ftp_port: int = 990`,
  `ftp_timeout_seconds: float = 30.0`, `ftp_max_fetch_bytes: int = 33554432`,
  `ftp_search_dirs: str = "/,/cache"`, and properties
  `cost_per_gram -> float`, `db_path -> Path`, `ftp_dirs -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_printer_readonly.py
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
```

```python
# appended to tests/test_config.py
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
    s = Settings()
    assert s.cost_per_gram == pytest.approx(0.03332)


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
    assert s.ftp_dirs == ["/", "/cache"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_config.py tests/test_printer_readonly.py -q`
Expected: config tests FAIL on missing `cost_per_gram`; readonly tests PASS
already (nothing violates the constraint yet), which is correct.

- [ ] **Step 3: Extend app/config.py**

```python
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
```

and the properties:

```python
    @property
    def cost_per_gram(self) -> float:
        if self.spool_weight_g <= 0:
            return 0.0
        return self.spool_cost / self.spool_weight_g

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bambu_watch.db"

    @property
    def ftp_dirs(self) -> list[str]:
        return [d.strip() for d in self.ftp_search_dirs.split(",") if d.strip()]
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, including the new readonly scan.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py tests/test_printer_readonly.py
git commit -m "Add cost and FTPS settings, and enforce printer read-only by test"
```

---

### Task 2: SQLite database layer

**Files:**
- Create: `app/storage/database.py`, `tests/storage/test_database.py`

**Interfaces:**
- Consumes: `Settings.db_path`.
- Produces: `SCHEMA_VERSION: int = 1`,
  `connect(path: Path) -> sqlite3.Connection`, `migrate(conn) -> None`.
  Connection has `row_factory = sqlite3.Row`, WAL journal, `synchronous=FULL`,
  and foreign keys on.

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_database.py
import sqlite3

from app.storage.database import SCHEMA_VERSION, connect


def test_connect_creates_schema(tmp_path):
    conn = connect(tmp_path / "x.db")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"prints", "print_filaments"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_connect_creates_parent_directory(tmp_path):
    conn = connect(tmp_path / "nested" / "deep" / "x.db")
    assert (tmp_path / "nested" / "deep" / "x.db").exists()
    conn.close()


def test_reopening_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    connect(path).close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_pragmas_are_set(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_rows_are_mappings(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute(
        "INSERT INTO prints (id, started_at, outcome) VALUES ('a', 'now', 'running')")
    row = conn.execute("SELECT id, outcome FROM prints").fetchone()
    assert row["id"] == "a"
    assert row["outcome"] == "running"


def test_filament_requires_a_parent_print(tmp_path):
    conn = connect(tmp_path / "x.db")
    with pytest_raises_integrity():
        conn.execute(
            "INSERT INTO print_filaments (print_id, slot) VALUES ('missing', 0)")


def pytest_raises_integrity():
    import pytest
    return pytest.raises(sqlite3.IntegrityError)


def test_filament_cascades_on_print_delete(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute(
        "INSERT INTO prints (id, started_at, outcome) VALUES ('a', 'now', 'running')")
    conn.execute("INSERT INTO print_filaments (print_id, slot) VALUES ('a', 0)")
    conn.execute("DELETE FROM prints WHERE id='a'")
    assert conn.execute("SELECT COUNT(*) FROM print_filaments").fetchone()[0] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/storage/test_database.py -q`
Expected: FAIL, `No module named 'app.storage.database'`

- [ ] **Step 3: Write app/storage/database.py**

```python
"""SQLite storage for print history.

One file on the data volume. sqlite3 is stdlib, so there is no new dependency
and no server process on the Pi. WAL so the database can be queried while the
service runs; synchronous FULL because writes happen twice per print and
power loss on a Pi is plausible, so durability beats speed at this volume.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS prints (
    id                  TEXT PRIMARY KEY,
    file_name           TEXT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    outcome             TEXT NOT NULL,
    final_state         TEXT,
    duration_seconds    INTEGER,
    progress_at_end     INTEGER,
    layer_at_end        INTEGER,
    total_layers        INTEGER,
    planned_grams       REAL,
    planned_meters      REAL,
    planned_cost        REAL,
    consumed_grams      REAL,
    consumed_cost       REAL,
    consumption_method  TEXT,
    slice_info_source   TEXT,
    cost_per_gram       REAL,
    checks              INTEGER NOT NULL DEFAULT 0,
    alerts              INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    vision_model        TEXT,
    session_dir         TEXT
);

CREATE INDEX IF NOT EXISTS idx_prints_started ON prints(started_at);
CREATE INDEX IF NOT EXISTS idx_prints_outcome ON prints(outcome);

CREATE TABLE IF NOT EXISTS print_filaments (
    print_id        TEXT NOT NULL REFERENCES prints(id) ON DELETE CASCADE,
    slot            INTEGER NOT NULL,
    filament_type   TEXT,
    color           TEXT,
    used_grams      REAL,
    used_meters     REAL,
    PRIMARY KEY (print_id, slot)
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    if version == 0:
        conn.executescript(SCHEMA_V1)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info("initialised print history schema v%d", SCHEMA_VERSION)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/storage/test_database.py -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add app/storage/database.py tests/storage/test_database.py
git commit -m "Add SQLite schema for print history"
```

---

### Task 3: Outcome and consumption, pure functions

Heaviest coverage in this plan. These decide the numbers the operator reads,
they are the place an estimate could silently masquerade as a measurement, and
they need no I/O.

**Files:**
- Create: `app/storage/records.py`, `tests/storage/test_records.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `OUTCOME_RUNNING/COMPLETED/STOPPED/FAILED/UNKNOWN` string constants.
  - `METHOD_COMPLETE = "complete"`, `METHOD_LAYER_FRACTION = "layer_fraction"`,
    `METHOD_UNKNOWN = "unknown"`.
  - `determine_outcome(final_state: str | None, progress: int | None) -> str`
  - `compute_consumption(planned_grams: float | None, layer_at_end: int | None,
    total_layers: int | None, outcome: str) -> tuple[float | None, str]`
  - `Consumption` NamedTuple alias is **not** used; the pair is returned plainly.

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_records.py
import pytest

from app.storage.records import (
    METHOD_COMPLETE,
    METHOD_LAYER_FRACTION,
    METHOD_UNKNOWN,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_STOPPED,
    OUTCOME_UNKNOWN,
    compute_consumption,
    determine_outcome,
)


@pytest.mark.parametrize("state,progress,expected", [
    ("FINISH", 100, OUTCOME_COMPLETED),
    ("FINISH", 42, OUTCOME_COMPLETED),
    ("FAILED", 55, OUTCOME_FAILED),
    ("IDLE", 100, OUTCOME_COMPLETED),
    ("IDLE", 99, OUTCOME_COMPLETED),
    ("IDLE", 98, OUTCOME_STOPPED),
    ("IDLE", 0, OUTCOME_STOPPED),
    ("IDLE", None, OUTCOME_UNKNOWN),
    ("PREPARE", 0, OUTCOME_UNKNOWN),
    (None, None, OUTCOME_UNKNOWN),
    ("SOMETHING_NEW", 50, OUTCOME_UNKNOWN),
])
def test_determine_outcome(state, progress, expected):
    assert determine_outcome(state, progress) == expected


def test_completed_print_consumes_everything_planned():
    grams, method = compute_consumption(340.0, 1240, 1240, OUTCOME_COMPLETED)
    assert grams == pytest.approx(340.0)
    assert method == METHOD_COMPLETE


def test_completed_print_ignores_layer_mismatch():
    grams, method = compute_consumption(340.0, 1200, 1240, OUTCOME_COMPLETED)
    assert grams == pytest.approx(340.0), "a finished print used its whole estimate"
    assert method == METHOD_COMPLETE


def test_stopped_print_uses_layer_fraction():
    grams, method = compute_consumption(340.0, 620, 1240, OUTCOME_STOPPED)
    assert grams == pytest.approx(170.0)
    assert method == METHOD_LAYER_FRACTION


def test_failed_print_uses_layer_fraction():
    grams, method = compute_consumption(1000.0, 883, 1240, OUTCOME_FAILED)
    assert grams == pytest.approx(712.09677, rel=1e-4)
    assert method == METHOD_LAYER_FRACTION


def test_no_planned_grams_yields_none_not_zero():
    grams, method = compute_consumption(None, 620, 1240, OUTCOME_STOPPED)
    assert grams is None, "unknown must not be reported as zero"
    assert method == METHOD_UNKNOWN


def test_missing_layer_data_yields_none():
    assert compute_consumption(340.0, None, 1240, OUTCOME_STOPPED) == (None, METHOD_UNKNOWN)
    assert compute_consumption(340.0, 620, None, OUTCOME_STOPPED) == (None, METHOD_UNKNOWN)


def test_zero_total_layers_does_not_divide_by_zero():
    assert compute_consumption(340.0, 5, 0, OUTCOME_STOPPED) == (None, METHOD_UNKNOWN)


def test_fraction_is_clamped():
    grams, _ = compute_consumption(340.0, 2000, 1240, OUTCOME_STOPPED)
    assert grams == pytest.approx(340.0), "cannot consume more than planned"
    grams, _ = compute_consumption(340.0, -5, 1240, OUTCOME_STOPPED)
    assert grams == pytest.approx(0.0)


def test_running_outcome_has_no_consumption_yet():
    from app.storage.records import OUTCOME_RUNNING
    assert compute_consumption(340.0, 100, 1240, OUTCOME_RUNNING) == (None, METHOD_UNKNOWN)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/storage/test_records.py -q`
Expected: FAIL, `No module named 'app.storage.records'`

- [ ] **Step 3: Write app/storage/records.py**

```python
"""Outcome and consumption derivation.

Pure functions with no I/O. These produce the numbers the operator reads, so
the distinction between a measurement and an estimate is encoded in the
returned method rather than left to the caller's memory.
"""

from __future__ import annotations

OUTCOME_RUNNING = "running"
OUTCOME_COMPLETED = "completed"
OUTCOME_STOPPED = "stopped"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"

METHOD_COMPLETE = "complete"
METHOD_LAYER_FRACTION = "layer_fraction"
METHOD_UNKNOWN = "unknown"

# A print this far along that drops to IDLE finished rather than being stopped.
COMPLETION_PROGRESS = 99


def determine_outcome(final_state: str | None, progress: int | None) -> str:
    """Classify how a print ended.

    Whether cancelling from Bambu Handy reports FAILED or drops to IDLE is not
    established, so an ambiguous close returns UNKNOWN rather than being
    confidently mislabelled. UNKNOWN is a real value, not an error.
    """
    if final_state == "FINISH":
        return OUTCOME_COMPLETED
    if final_state == "FAILED":
        return OUTCOME_FAILED
    if final_state == "IDLE":
        if progress is None:
            return OUTCOME_UNKNOWN
        return (
            OUTCOME_COMPLETED if progress >= COMPLETION_PROGRESS else OUTCOME_STOPPED
        )
    return OUTCOME_UNKNOWN


def compute_consumption(
    planned_grams: float | None,
    layer_at_end: int | None,
    total_layers: int | None,
    outcome: str,
) -> tuple[float | None, str]:
    """Return (grams consumed, method).

    A completed print consumed its whole estimate. A print that stopped partway
    is scaled by layer fraction, which is an ESTIMATE: layer count measures
    height, not volume, so a part whose cross-section varies with height will
    be wrong in proportion to that variation. The returned method says so, and
    callers must not present a layer_fraction figure as exact.
    """
    if planned_grams is None:
        return None, METHOD_UNKNOWN

    if outcome == OUTCOME_COMPLETED:
        return planned_grams, METHOD_COMPLETE

    if outcome == OUTCOME_RUNNING:
        return None, METHOD_UNKNOWN

    if not layer_at_end or not total_layers or total_layers <= 0:
        return None, METHOD_UNKNOWN

    fraction = min(1.0, max(0.0, layer_at_end / total_layers))
    return planned_grams * fraction, METHOD_LAYER_FRACTION
```

Note `not layer_at_end` treats layer 0 as missing, which is correct: a print
that died before its first layer consumed nothing worth recording and has no
reliable figure.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/storage/test_records.py -q`
Expected: 20 passed

- [ ] **Step 5: Commit**

```bash
git add app/storage/records.py tests/storage/test_records.py
git commit -m "Add outcome and consumption derivation"
```

---

### Task 4: Print repository

**Files:**
- Create: `app/storage/prints.py`, `tests/storage/test_prints.py`

**Interfaces:**
- Consumes: `connect` (Task 2), records constants (Task 3), `Settings`.
- Produces:
  - `FilamentRow` dataclass: `slot: int`, `filament_type: str | None`,
    `color: str | None`, `used_grams: float | None`, `used_meters: float | None`.
  - `PrintRepository` with `__init__(self, conn, settings)`,
    `insert_running(self, print_id, file_name, started_at, session_dir) -> None`,
    `finalize(self, print_id, *, final_state, ended_at, progress, layer, total_layers, planned_grams, planned_meters, checks, alerts, input_tokens, output_tokens) -> str`
    returning the computed outcome,
    `save_filaments(self, print_id, rows: list[FilamentRow]) -> None`,
    `set_slice_source(self, print_id, source) -> None`,
    `reconcile_stale(self) -> int`,
    `get(self, print_id) -> sqlite3.Row | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/storage/test_prints.py
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.storage.database import connect
from app.storage.prints import FilamentRow, PrintRepository
from app.storage.records import (
    METHOD_COMPLETE,
    METHOD_LAYER_FRACTION,
    OUTCOME_COMPLETED,
    OUTCOME_RUNNING,
    OUTCOME_STOPPED,
    OUTCOME_UNKNOWN,
)

START = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)


def make_settings(tmp_path, **over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u", data_dir=tmp_path)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def repo(tmp_path):
    s = make_settings(tmp_path)
    return PrintRepository(connect(s.db_path), s)


def insert(repo, print_id="p1", name="Mask_Final_v17"):
    repo.insert_running(print_id, name, START, "data/sessions/x")
    return print_id


def finalize(repo, print_id, **over):
    kw = dict(final_state="FINISH", ended_at=START + timedelta(hours=4),
              progress=100, layer=1240, total_layers=1240,
              planned_grams=340.0, planned_meters=113.2,
              checks=320, alerts=0, input_tokens=496000, output_tokens=96000)
    kw.update(over)
    return repo.finalize(print_id, **kw)


def test_insert_running_creates_row(repo):
    insert(repo)
    row = repo.get("p1")
    assert row["outcome"] == OUTCOME_RUNNING
    assert row["file_name"] == "Mask_Final_v17"
    assert row["ended_at"] is None
    assert row["cost_per_gram"] == pytest.approx(0.013)


def test_insert_is_idempotent_on_restart(repo):
    insert(repo)
    insert(repo)
    assert repo.get("p1") is not None


def test_finalize_completed_print(repo):
    insert(repo)
    assert finalize(repo) == OUTCOME_COMPLETED
    row = repo.get("p1")
    assert row["outcome"] == OUTCOME_COMPLETED
    assert row["duration_seconds"] == 4 * 3600
    assert row["planned_cost"] == pytest.approx(4.42)
    assert row["consumed_grams"] == pytest.approx(340.0)
    assert row["consumed_cost"] == pytest.approx(4.42)
    assert row["consumption_method"] == METHOD_COMPLETE
    assert row["checks"] == 320


def test_finalize_stopped_print_estimates_consumption(repo):
    insert(repo)
    outcome = finalize(repo, final_state="IDLE", progress=50, layer=620)
    assert outcome == OUTCOME_STOPPED
    row = repo.get("p1")
    assert row["consumed_grams"] == pytest.approx(170.0)
    assert row["consumed_cost"] == pytest.approx(2.21)
    assert row["consumption_method"] == METHOD_LAYER_FRACTION
    assert row["planned_cost"] == pytest.approx(4.42), "planned is unchanged"


def test_finalize_without_slice_info_records_no_material(repo):
    insert(repo)
    finalize(repo, planned_grams=None, planned_meters=None)
    row = repo.get("p1")
    assert row["planned_grams"] is None
    assert row["planned_cost"] is None
    assert row["consumed_grams"] is None, "unknown must not read as zero"


def test_cost_per_gram_is_snapshotted_per_print(tmp_path):
    s1 = make_settings(tmp_path)
    repo1 = PrintRepository(connect(s1.db_path), s1)
    insert(repo1)
    finalize(repo1, "p1")

    s2 = make_settings(tmp_path, spool_cost=26.0)
    repo2 = PrintRepository(connect(s2.db_path), s2)
    assert repo2.get("p1")["cost_per_gram"] == pytest.approx(0.013), (
        "history must not shift when the spool price changes"
    )
    insert(repo2, "p2")
    assert repo2.get("p2")["cost_per_gram"] == pytest.approx(0.026)


def test_save_filaments_records_every_slot(repo):
    insert(repo)
    repo.save_filaments("p1", [
        FilamentRow(0, "PLA", "#FF0000", 120.0, 40.0),
        FilamentRow(1, "PLA", "#00FF00", 95.5, 31.8),
        FilamentRow(2, "PETG", "#0000FF", 80.0, 26.0),
        FilamentRow(3, "PLA", "#FFFFFF", 44.5, 15.4),
    ])
    rows = repo.filaments("p1")
    assert len(rows) == 4
    assert [r["slot"] for r in rows] == [0, 1, 2, 3]
    assert {r["color"] for r in rows} == {"#FF0000", "#00FF00", "#0000FF", "#FFFFFF"}
    assert sum(r["used_grams"] for r in rows) == pytest.approx(340.0)


def test_save_filaments_replaces_rather_than_duplicates(repo):
    insert(repo)
    repo.save_filaments("p1", [FilamentRow(0, "PLA", "#FF0000", 120.0, 40.0)])
    repo.save_filaments("p1", [FilamentRow(0, "PLA", "#FF0000", 130.0, 43.0)])
    rows = repo.filaments("p1")
    assert len(rows) == 1
    assert rows[0]["used_grams"] == pytest.approx(130.0)


def test_external_spool_uses_slot_minus_one(repo):
    insert(repo)
    repo.save_filaments("p1", [FilamentRow(-1, "PLA", "#123456", 50.0, 17.0)])
    assert repo.filaments("p1")[0]["slot"] == -1


def test_reconcile_stale_marks_orphaned_running_rows(repo):
    insert(repo, "p1")
    insert(repo, "p2")
    finalize(repo, "p2")
    assert repo.reconcile_stale() == 1
    assert repo.get("p1")["outcome"] == OUTCOME_UNKNOWN
    assert repo.get("p2")["outcome"] == OUTCOME_COMPLETED


def test_reconcile_stale_is_a_noop_when_clean(repo):
    insert(repo)
    finalize(repo, "p1")
    assert repo.reconcile_stale() == 0


def test_set_slice_source(repo):
    insert(repo)
    repo.set_slice_source("p1", "unavailable")
    assert repo.get("p1")["slice_info_source"] == "unavailable"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/storage/test_prints.py -q`
Expected: FAIL, `No module named 'app.storage.prints'`

- [ ] **Step 3: Write app/storage/prints.py**

```python
"""Print row lifecycle.

Rows are inserted when monitoring starts and updated at close, so a print
survives the service being killed or the Pi losing power. cost_per_gram is
snapshotted onto each row so history stays accurate after a price change.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.storage.records import (
    OUTCOME_RUNNING,
    OUTCOME_UNKNOWN,
    compute_consumption,
    determine_outcome,
)

logger = logging.getLogger(__name__)


@dataclass
class FilamentRow:
    slot: int
    filament_type: str | None = None
    color: str | None = None
    used_grams: float | None = None
    used_meters: float | None = None


class PrintRepository:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings

    def insert_running(
        self,
        print_id: str,
        file_name: str | None,
        started_at: datetime,
        session_dir: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO prints
                (id, file_name, started_at, outcome, cost_per_gram,
                 vision_model, session_dir)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                print_id,
                file_name,
                started_at.isoformat(),
                OUTCOME_RUNNING,
                self.settings.cost_per_gram,
                self.settings.vision_model,
                session_dir,
            ),
        )

    def finalize(
        self,
        print_id: str,
        *,
        final_state: str | None,
        ended_at: datetime,
        progress: int | None,
        layer: int | None,
        total_layers: int | None,
        planned_grams: float | None,
        planned_meters: float | None,
        checks: int,
        alerts: int,
        input_tokens: int,
        output_tokens: int,
    ) -> str:
        row = self.get(print_id)
        rate = row["cost_per_gram"] if row else self.settings.cost_per_gram
        started = (
            datetime.fromisoformat(row["started_at"]) if row else None
        )
        duration = (
            int((ended_at - started).total_seconds()) if started else None
        )

        outcome = determine_outcome(final_state, progress)
        consumed_grams, method = compute_consumption(
            planned_grams, layer, total_layers, outcome
        )

        planned_cost = None if planned_grams is None else planned_grams * rate
        consumed_cost = None if consumed_grams is None else consumed_grams * rate

        self.conn.execute(
            """
            UPDATE prints SET
                ended_at = ?, outcome = ?, final_state = ?, duration_seconds = ?,
                progress_at_end = ?, layer_at_end = ?, total_layers = ?,
                planned_grams = ?, planned_meters = ?, planned_cost = ?,
                consumed_grams = ?, consumed_cost = ?, consumption_method = ?,
                checks = ?, alerts = ?, input_tokens = ?, output_tokens = ?
            WHERE id = ?
            """,
            (
                ended_at.isoformat(), outcome, final_state, duration,
                progress, layer, total_layers,
                planned_grams, planned_meters, planned_cost,
                consumed_grams, consumed_cost, method,
                checks, alerts, input_tokens, output_tokens,
                print_id,
            ),
        )
        return outcome

    def save_filaments(self, print_id: str, rows: list[FilamentRow]) -> None:
        self.conn.execute("DELETE FROM print_filaments WHERE print_id = ?", (print_id,))
        self.conn.executemany(
            """
            INSERT INTO print_filaments
                (print_id, slot, filament_type, color, used_grams, used_meters)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (print_id, r.slot, r.filament_type, r.color, r.used_grams, r.used_meters)
                for r in rows
            ],
        )

    def set_slice_source(self, print_id: str, source: str) -> None:
        self.conn.execute(
            "UPDATE prints SET slice_info_source = ? WHERE id = ?", (source, print_id)
        )

    def reconcile_stale(self) -> int:
        """A row still marked running at startup belongs to a print the
        service did not see finish. Record that honestly rather than leaving
        it looking live."""
        cursor = self.conn.execute(
            "UPDATE prints SET outcome = ? WHERE outcome = ?",
            (OUTCOME_UNKNOWN, OUTCOME_RUNNING),
        )
        count = cursor.rowcount or 0
        if count:
            logger.info("reconciled %d interrupted print(s) to unknown", count)
        return count

    def get(self, print_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM prints WHERE id = ?", (print_id,)
        ).fetchone()

    def filaments(self, print_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM print_filaments WHERE print_id = ? ORDER BY slot",
                (print_id,),
            )
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/storage -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add app/storage/prints.py tests/storage/test_prints.py
git commit -m "Add print repository with start-and-close row lifecycle"
```

---

### Task 5: Slice info parsing, no I/O

**Files:**
- Create: `app/bambu/slice_info.py`, `tests/bambu/test_slice_info.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `FilamentUsage` dataclass: `slot: int`, `filament_type: str | None`,
    `color: str | None`, `used_grams: float | None`, `used_meters: float | None`.
  - `SliceInfo` dataclass: `filaments: list[FilamentUsage]`, with properties
    `total_grams -> float | None`, `total_meters -> float | None`.
  - `SLICE_INFO_MEMBER = "Metadata/slice_info.config"`
  - `parse_slice_info_xml(data: bytes) -> SliceInfo | None`
  - `extract_slice_info(archive: bytes) -> SliceInfo | None`
  - `normalize_color(raw: str | None) -> str | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/bambu/test_slice_info.py
import io
import zipfile

import pytest

from app.bambu.slice_info import (
    SLICE_INFO_MEMBER,
    extract_slice_info,
    normalize_color,
    parse_slice_info_xml,
)

FOUR_COLOUR = b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="1" type="PLA"  color="#FF0000" used_m="40.0" used_g="120.0"/>
    <filament id="2" type="PLA"  color="#00FF00" used_m="31.8" used_g="95.5"/>
    <filament id="3" type="PETG" color="#0000FF" used_m="26.0" used_g="80.0"/>
    <filament id="4" type="PLA"  color="#FFFFFF" used_m="15.4" used_g="44.5"/>
  </plate>
</config>
"""

SINGLE = b"""<?xml version="1.0"?>
<config><plate>
  <filament id="1" type="PLA" color="000000FF" used_m="113.2" used_g="340.0"/>
</plate></config>
"""


def make_3mf(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_parses_four_filaments():
    info = parse_slice_info_xml(FOUR_COLOUR)
    assert len(info.filaments) == 4
    assert [f.slot for f in info.filaments] == [1, 2, 3, 4]
    assert info.filaments[2].filament_type == "PETG"
    assert info.total_grams == pytest.approx(340.0)
    assert info.total_meters == pytest.approx(113.2)


def test_colors_are_normalized():
    assert normalize_color("#FF0000") == "#FF0000"
    assert normalize_color("000000FF") == "#000000", "RGBA hex drops alpha"
    assert normalize_color("ff0000") == "#FF0000"
    assert normalize_color(None) is None
    assert normalize_color("") is None
    assert normalize_color("not a colour") is None


def test_single_filament_rgba_color():
    info = parse_slice_info_xml(SINGLE)
    assert len(info.filaments) == 1
    assert info.filaments[0].color == "#000000"
    assert info.total_grams == pytest.approx(340.0)


def test_malformed_xml_returns_none():
    assert parse_slice_info_xml(b"<config><unclosed>") is None
    assert parse_slice_info_xml(b"") is None
    assert parse_slice_info_xml(b"not xml at all") is None


def test_no_filament_elements_returns_none():
    assert parse_slice_info_xml(b"<config><plate/></config>") is None


def test_missing_numbers_do_not_crash():
    info = parse_slice_info_xml(
        b'<config><plate><filament id="1" type="PLA"/></plate></config>')
    assert info.filaments[0].used_grams is None
    assert info.total_grams is None, "no data must not total to zero"


def test_unparseable_numbers_become_none():
    info = parse_slice_info_xml(
        b'<config><plate><filament id="1" used_g="abc" used_m="-"/></plate></config>')
    assert info.filaments[0].used_grams is None


def test_filaments_without_ids_are_numbered_by_order():
    info = parse_slice_info_xml(
        b'<config><plate><filament used_g="1"/><filament used_g="2"/></plate></config>')
    assert [f.slot for f in info.filaments] == [1, 2], "primary key must stay unique"


def test_duplicate_ids_are_rejected_entirely():
    info = parse_slice_info_xml(
        b'<config><plate><filament id="1" used_g="1"/>'
        b'<filament id="1" used_g="2"/></plate></config>')
    assert info is None, "a colliding key means the file is malformed"


def test_extract_from_3mf():
    archive = make_3mf({
        "3D/3dmodel.model": b"<model/>",
        SLICE_INFO_MEMBER: FOUR_COLOUR,
    })
    info = extract_slice_info(archive)
    assert len(info.filaments) == 4


def test_extract_missing_member_returns_none():
    assert extract_slice_info(make_3mf({"3D/3dmodel.model": b"<model/>"})) is None


def test_extract_from_non_zip_returns_none():
    assert extract_slice_info(b"definitely not a zip") is None


def test_extract_from_truncated_zip_returns_none():
    archive = make_3mf({SLICE_INFO_MEMBER: FOUR_COLOUR})
    assert extract_slice_info(archive[: len(archive) // 2]) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/bambu/test_slice_info.py -q`
Expected: FAIL, `No module named 'app.bambu.slice_info'`

- [ ] **Step 3: Write app/bambu/slice_info.py**

```python
"""Parse filament usage out of a sliced 3MF.

Deliberately separated from the FTPS transport so all parsing is testable
against a synthetic archive built in-process. Everything returns None on bad
input rather than raising: a malformed file means no material figures, never a
broken monitoring session.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

SLICE_INFO_MEMBER = "Metadata/slice_info.config"
_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")


def normalize_color(raw: str | None) -> str | None:
    """Slicers report colours as #RRGGBB or bare RRGGBBAA. Normalize to
    #RRGGBB and drop any alpha channel."""
    if not raw:
        return None
    match = _HEX.match(raw.strip())
    if not match:
        return None
    return f"#{match.group(1).upper()}"


def _as_float(raw: str | None) -> float | None:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class FilamentUsage:
    slot: int
    filament_type: str | None = None
    color: str | None = None
    used_grams: float | None = None
    used_meters: float | None = None


@dataclass
class SliceInfo:
    filaments: list[FilamentUsage] = field(default_factory=list)

    @property
    def total_grams(self) -> float | None:
        values = [f.used_grams for f in self.filaments if f.used_grams is not None]
        return sum(values) if values else None

    @property
    def total_meters(self) -> float | None:
        values = [f.used_meters for f in self.filaments if f.used_meters is not None]
        return sum(values) if values else None


def parse_slice_info_xml(data: bytes) -> SliceInfo | None:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        logger.warning("slice info is not parseable XML")
        return None

    elements = root.iter("filament")
    filaments: list[FilamentUsage] = []
    seen: set[int] = set()

    for index, element in enumerate(elements, start=1):
        slot = _as_float(element.get("id"))
        # Filaments without an id are numbered by order of appearance so the
        # (print_id, slot) primary key stays unique.
        resolved = int(slot) if slot is not None else index
        if resolved in seen:
            logger.warning("slice info has duplicate filament id %s", resolved)
            return None
        seen.add(resolved)

        filaments.append(
            FilamentUsage(
                slot=resolved,
                filament_type=element.get("type") or None,
                color=normalize_color(element.get("color")),
                used_grams=_as_float(element.get("used_g")),
                used_meters=_as_float(element.get("used_m")),
            )
        )

    if not filaments:
        logger.warning("slice info contains no filament entries")
        return None
    return SliceInfo(filaments=filaments)


def extract_slice_info(archive: bytes) -> SliceInfo | None:
    """Pull SLICE_INFO_MEMBER out of a 3MF, which is a ZIP."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            data = zf.read(SLICE_INFO_MEMBER)
    except (zipfile.BadZipFile, KeyError, OSError, EOFError) as exc:
        logger.warning("could not read %s: %s", SLICE_INFO_MEMBER, exc)
        return None
    return parse_slice_info_xml(data)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/bambu/test_slice_info.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add app/bambu/slice_info.py tests/bambu/test_slice_info.py
git commit -m "Add 3MF slice info parsing"
```

---

### Task 6: Read-only FTPS client

**Files:**
- Create: `app/bambu/ftp_client.py`, `tests/bambu/test_ftp_client.py`

**Interfaces:**
- Consumes: `Settings`, `SliceInfo`/`extract_slice_info` (Task 5).
- Produces: `BambuFtpClient` with `__init__(self, settings)` and
  `async fetch_slice_info(self, file_name: str) -> SliceInfo | None`.
  Internals exposed for test: `candidate_names(file_name) -> list[str]`,
  `_retrieve(ftp, path) -> bytes | None`.
  **No write method of any kind.**

- [ ] **Step 1: Write the failing test**

```python
# tests/bambu/test_ftp_client.py
import io
import zipfile

import pytest

from app.bambu.ftp_client import BambuFtpClient, candidate_names
from app.bambu.slice_info import SLICE_INFO_MEMBER
from app.config import Settings


def make_settings(**over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u")
    base.update(over)
    return Settings(**base)


SLICE_XML = (b'<config><plate><filament id="1" type="PLA" color="#FF0000" '
             b'used_m="113.2" used_g="340.0"/></plate></config>')


def make_3mf() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(SLICE_INFO_MEMBER, SLICE_XML)
    return buf.getvalue()


def test_candidate_names_covers_common_spellings():
    names = candidate_names("Mask_Final_v17")
    assert "Mask_Final_v17.3mf" in names
    assert "Mask_Final_v17.gcode.3mf" in names


def test_candidate_names_does_not_double_the_extension():
    names = candidate_names("Mask_Final_v17.3mf")
    assert "Mask_Final_v17.3mf" in names
    assert "Mask_Final_v17.3mf.3mf" not in names


def test_candidate_names_strips_metadata_prefix():
    names = candidate_names("Metadata/plate_1.gcode")
    assert all("/" not in n for n in names), "only a basename can be fetched"


def test_client_exposes_no_write_methods():
    client = BambuFtpClient(make_settings())
    for attr in dir(client):
        assert not attr.startswith(("store", "upload", "delete", "rename", "mkdir"))


async def test_fetch_returns_slice_info(monkeypatch):
    client = BambuFtpClient(make_settings())
    monkeypatch.setattr(client, "_fetch_archive", lambda name: make_3mf())
    info = await client.fetch_slice_info("Mask_Final_v17")
    assert info is not None
    assert info.total_grams == pytest.approx(340.0)
    assert info.filaments[0].color == "#FF0000"


async def test_fetch_returns_none_when_archive_missing(monkeypatch):
    client = BambuFtpClient(make_settings())
    monkeypatch.setattr(client, "_fetch_archive", lambda name: None)
    assert await client.fetch_slice_info("x") is None


async def test_fetch_returns_none_on_transport_error(monkeypatch):
    client = BambuFtpClient(make_settings())

    def boom(name):
        raise OSError("connection refused")

    monkeypatch.setattr(client, "_fetch_archive", boom)
    assert await client.fetch_slice_info("x") is None, (
        "an FTPS failure must never propagate into the monitor"
    )


async def test_fetch_rejects_empty_file_name():
    client = BambuFtpClient(make_settings())
    assert await client.fetch_slice_info("") is None


async def test_fetch_honours_the_master_switch(monkeypatch):
    client = BambuFtpClient(make_settings(enable_slice_fetch=False))
    called = []
    monkeypatch.setattr(client, "_fetch_archive", lambda n: called.append(n))
    assert await client.fetch_slice_info("x") is None
    assert called == [], "no connection may be opened when disabled"


def test_size_cap_aborts_a_large_transfer():
    client = BambuFtpClient(make_settings(ftp_max_fetch_bytes=10))

    class FakeFtp:
        def retrbinary(self, cmd, callback, blocksize=8192):
            for _ in range(5):
                callback(b"xxxx")

    assert client._retrieve(FakeFtp(), "/x.3mf") is None


def test_retrieve_returns_bytes_under_the_cap():
    client = BambuFtpClient(make_settings())

    class FakeFtp:
        def retrbinary(self, cmd, callback, blocksize=8192):
            callback(b"hello")
            callback(b"world")

    assert client._retrieve(FakeFtp(), "/x.3mf") == b"helloworld"


def test_retrieve_issues_only_a_retr_command():
    client = BambuFtpClient(make_settings())
    issued = []

    class FakeFtp:
        def retrbinary(self, cmd, callback, blocksize=8192):
            issued.append(cmd)
            callback(b"x")

    client._retrieve(FakeFtp(), "/x.3mf")
    assert issued == ["RETR /x.3mf"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/bambu/test_ftp_client.py -q`
Expected: FAIL, `No module named 'app.bambu.ftp_client'`

- [ ] **Step 3: Write app/bambu/ftp_client.py**

```python
"""Read-only FTPS access to the printer's SD card.

RETRIEVAL ONLY. No write verb is wrapped anywhere in this module, and
tests/test_printer_readonly.py scans the source to keep it that way. The risk
being managed is not accidental writes but SD card I/O contention, so the
caller is responsible for never invoking this while the printer is RUNNING.

Bambu printers serve FTPS on 990 with implicit TLS. ftplib.FTP_TLS does
explicit TLS by default, hence the socket-wrapping subclass. The certificate
is self-signed, so verification is disabled, as with MQTT and the camera.
"""

from __future__ import annotations

import asyncio
import ftplib
import logging
import socket
import ssl
from pathlib import Path

from app.bambu.slice_info import SliceInfo, extract_slice_info
from app.config import Settings

logger = logging.getLogger(__name__)


class ImplicitFtpTls(ftplib.FTP_TLS):
    """FTP_TLS that negotiates TLS on connect rather than after AUTH."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=None)
        self._sock = value


def candidate_names(file_name: str) -> list[str]:
    """Plausible archive names for a print. MQTT reports either a friendly
    subtask_name with no extension or a path like Metadata/plate_1.gcode, so
    try the obvious spellings. Only basenames: no traversal."""
    base = Path(file_name.strip()).name
    if not base:
        return []

    stem = base
    for suffix in (".3mf", ".gcode"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]

    names = [base] if base.lower().endswith(".3mf") else []
    names += [f"{stem}.3mf", f"{stem}.gcode.3mf"]

    seen: set[str] = set()
    return [n for n in names if not (n in seen or seen.add(n))]


class BambuFtpClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def fetch_slice_info(self, file_name: str) -> SliceInfo | None:
        """Fetch and parse slice info. Returns None on any failure.

        MUST NOT be called while the printer reports RUNNING.
        """
        if not self.settings.enable_slice_fetch:
            return None
        if not file_name or not file_name.strip():
            return None

        loop = asyncio.get_running_loop()
        try:
            archive = await loop.run_in_executor(
                None, self._fetch_archive, file_name
            )
        except Exception as exc:
            logger.warning("slice info fetch failed: %s", exc)
            return None

        if not archive:
            return None
        return extract_slice_info(archive)

    # --- blocking, runs in an executor ---

    def _fetch_archive(self, file_name: str) -> bytes | None:
        names = candidate_names(file_name)
        if not names:
            return None

        try:
            ftp = self._connect()
        except (ftplib.all_errors, ssl.SSLError, OSError) as exc:
            logger.warning("FTPS connect failed: %s", exc)
            return None

        try:
            for directory in self.settings.ftp_dirs:
                for name in names:
                    path = f"{directory.rstrip('/')}/{name}"
                    data = self._retrieve(ftp, path)
                    if data:
                        logger.info("fetched slice archive from %s", path)
                        return data
            logger.info("no sliced archive found for %r", file_name)
            return None
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    def _connect(self) -> ImplicitFtpTls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        ftp = ImplicitFtpTls(context=context)
        ftp.timeout = self.settings.ftp_timeout_seconds
        ftp.connect(
            host=self.settings.bambu_host,
            port=self.settings.bambu_ftp_port,
            timeout=self.settings.ftp_timeout_seconds,
        )
        ftp.login(user="bblp", passwd=self.settings.bambu_access_code)
        ftp.prot_p()
        return ftp

    def _retrieve(self, ftp, path: str) -> bytes | None:
        """RETR one file, aborting past the size cap. Returns None if the
        file is absent or too large."""
        chunks: list[bytes] = []
        total = 0
        cap = self.settings.ftp_max_fetch_bytes
        overflow = False

        def collect(chunk: bytes) -> None:
            nonlocal total, overflow
            if overflow:
                return
            total += len(chunk)
            if total > cap:
                overflow = True
                chunks.clear()
                return
            chunks.append(chunk)

        try:
            ftp.retrbinary(f"RETR {path}", collect)
        except (ftplib.all_errors, OSError, socket.timeout) as exc:
            logger.debug("RETR %s failed: %s", path, exc)
            return None

        if overflow:
            logger.warning("aborted %s: larger than %d bytes", path, cap)
            return None
        return b"".join(chunks) or None
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/bambu -q && .venv/bin/python -m pytest tests/test_printer_readonly.py -q`
Expected: all pass, including the read-only scan against the new module.

- [ ] **Step 5: Commit**

```bash
git add app/bambu/ftp_client.py tests/bambu/test_ftp_client.py
git commit -m "Add read-only FTPS client for sliced files"
```

---

### Task 7: AMS block parsing

**Files:**
- Create: `app/bambu/ams.py`, `tests/bambu/test_ams.py`
- Modify: `app/bambu/models.py` to retain the latest AMS slots

**Interfaces:**
- Consumes: `normalize_color` (Task 5).
- Produces:
  - `AmsSlot` dataclass: `slot: int`, `filament_type: str | None`,
    `color: str | None`, `remain: int | None`.
  - `parse_ams(print_data: dict) -> list[AmsSlot]`
  - `PrinterState.ams_slots: list[AmsSlot]`, updated by `apply_report`.

- [ ] **Step 1: Write the failing test**

```python
# tests/bambu/test_ams.py
from app.bambu.ams import parse_ams
from app.bambu.models import PrinterState

REPORT = {
    "ams": {
        "ams": [{
            "id": "0",
            "tray": [
                {"id": "0", "tray_type": "PLA",  "tray_color": "FF0000FF", "remain": 84},
                {"id": "1", "tray_type": "PLA",  "tray_color": "00FF00FF", "remain": 60},
                {"id": "2", "tray_type": "PETG", "tray_color": "0000FFFF", "remain": 12},
                {"id": "3", "tray_type": "PLA",  "tray_color": "FFFFFFFF", "remain": -1},
            ],
        }],
    },
    "vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "123456FF", "remain": 50},
}


def test_parses_four_ams_trays_and_external():
    slots = parse_ams(REPORT)
    by_slot = {s.slot: s for s in slots}
    assert {0, 1, 2, 3, -1} <= set(by_slot)
    assert by_slot[0].color == "#FF0000"
    assert by_slot[2].filament_type == "PETG"
    assert by_slot[-1].color == "#123456", "external spool is slot -1"


def test_unknown_remain_becomes_none():
    slots = {s.slot: s for s in parse_ams(REPORT)}
    assert slots[3].remain is None, "-1 means unknown, not zero"
    assert slots[0].remain == 84


def test_second_ams_unit_offsets_slot_numbers():
    report = {"ams": {"ams": [
        {"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]},
        {"id": "1", "tray": [{"id": "0", "tray_type": "ABS"}]},
    ]}}
    by_slot = {s.slot: s for s in parse_ams(report)}
    assert by_slot[0].filament_type == "PLA"
    assert by_slot[4].filament_type == "ABS", "unit 1 tray 0 is slot 4"


def test_empty_and_malformed_reports_yield_nothing():
    assert parse_ams({}) == []
    assert parse_ams({"ams": None}) == []
    assert parse_ams({"ams": {"ams": "not a list"}}) == []
    assert parse_ams({"ams": {"ams": [{"tray": "nope"}]}}) == []


def test_empty_tray_slot_is_skipped():
    report = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}}
    assert parse_ams(report) == [], "a slot with no filament is not a colour"


def test_printer_state_retains_ams_slots():
    state = PrinterState()
    state.apply_report(REPORT)
    assert len(state.ams_slots) == 5


def test_printer_state_ams_survives_incremental_reports():
    state = PrinterState()
    state.apply_report(REPORT)
    state.apply_report({"layer_num": 5})
    assert len(state.ams_slots) == 5, "absent ams block must not clear slots"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/bambu/test_ams.py -q`
Expected: FAIL, `No module named 'app.bambu.ams'`

- [ ] **Step 3: Write app/bambu/ams.py**

```python
"""Normalize the MQTT AMS block into flat slots.

Slot numbering is unit * 4 + tray so a second AMS unit does not collide with
the first. The external spool is slot -1, matching print_filaments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.bambu.slice_info import normalize_color

logger = logging.getLogger(__name__)

TRAYS_PER_UNIT = 4
EXTERNAL_SLOT = -1


@dataclass
class AmsSlot:
    slot: int
    filament_type: str | None = None
    color: str | None = None
    remain: int | None = None


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _slot_from(tray: dict, slot: int) -> AmsSlot | None:
    filament_type = tray.get("tray_type") or None
    color = normalize_color(tray.get("tray_color"))
    if not filament_type and not color:
        return None

    remain = _as_int(tray.get("remain"))
    if remain is not None and remain < 0:
        remain = None  # -1 means unknown, not empty
    return AmsSlot(slot=slot, filament_type=filament_type, color=color, remain=remain)


def parse_ams(print_data: dict) -> list[AmsSlot]:
    """Never raises. An unexpected shape yields an empty list."""
    slots: list[AmsSlot] = []

    block = print_data.get("ams")
    units = block.get("ams") if isinstance(block, dict) else None
    if isinstance(units, list):
        for index, unit in enumerate(units):
            if not isinstance(unit, dict):
                continue
            unit_id = _as_int(unit.get("id"))
            base = (unit_id if unit_id is not None else index) * TRAYS_PER_UNIT
            trays = unit.get("tray")
            if not isinstance(trays, list):
                continue
            for tray_index, tray in enumerate(trays):
                if not isinstance(tray, dict):
                    continue
                tray_id = _as_int(tray.get("id"))
                number = base + (tray_id if tray_id is not None else tray_index)
                parsed = _slot_from(tray, number)
                if parsed is not None:
                    slots.append(parsed)

    external = print_data.get("vt_tray")
    if isinstance(external, dict):
        parsed = _slot_from(external, EXTERNAL_SLOT)
        if parsed is not None:
            slots.append(parsed)

    return slots
```

- [ ] **Step 4: Modify app/bambu/models.py**

Add the import, the field, and the incremental-safe update:

```python
from app.bambu.ams import AmsSlot, parse_ams
```

```python
    ams_slots: list[AmsSlot] = field(default_factory=list)
```

and inside `apply_report`, before the filename block:

```python
        # Only replace slots when the report actually carries an AMS block;
        # reports are incremental.
        if "ams" in print_data or "vt_tray" in print_data:
            parsed = parse_ams(print_data)
            if parsed:
                self.ams_slots = parsed
```

`field` must be added to the existing `dataclasses` import.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/bambu -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add app/bambu/ams.py app/bambu/models.py tests/bambu/test_ams.py
git commit -m "Add AMS block parsing for slot colours"
```

---

### Task 8: Monitor integration

**Files:**
- Modify: `app/detection/monitor.py`
- Test: `tests/detection/test_monitor_db.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: `PrintMonitor.__init__` gains `repository=None` and `ftp=None`
  keyword arguments. `run_once` gains the `"preparing"` outcome.
  `_end_session` becomes `async`.

- [ ] **Step 1: Write the failing test**

```python
# tests/detection/test_monitor_db.py
from datetime import datetime, timezone

import pytest

from app.bambu.models import ImageFrame, PrinterState
from app.bambu.slice_info import FilamentUsage, SliceInfo
from app.config import Settings
from app.detection.monitor import PrintMonitor
from app.storage.database import connect
from app.storage.prints import PrintRepository
from app.storage.records import OUTCOME_COMPLETED, OUTCOME_STOPPED
from app.vision.analyzer import AnalysisResult


def make_settings(tmp_path, **over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u", data_dir=tmp_path)
    base.update(over)
    return Settings(**base)


class StubPrinter:
    def __init__(self, state):
        self.state = state
    def connect(self): ...
    def disconnect(self): ...


class StubCamera:
    async def capture(self):
        return ImageFrame(timestamp=datetime.now(timezone.utc), jpeg=b"\xff\xd8x\xff\xd9")


class StubAnalyzer:
    async def analyze(self, frames, state, interval):
        return AnalysisResult(analysis=None, input_tokens=100, output_tokens=20,
                              model="claude-sonnet-5")


class StubNotifier:
    async def send_failure(self, *a, **kw):
        return True


class StubFtp:
    def __init__(self, info=None, fail=False):
        self.info = info
        self.fail = fail
        self.calls = []
        self.states_when_called = []

    async def fetch_slice_info(self, file_name):
        self.calls.append(file_name)
        if self.fail:
            return None
        return self.info


def slice_info() -> SliceInfo:
    return SliceInfo(filaments=[
        FilamentUsage(1, "PLA", "#FF0000", 200.0, 66.0),
        FilamentUsage(2, "PLA", "#00FF00", 140.0, 47.2),
    ])


def running() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 10, "layer_num": 5,
                    "total_layer_num": 100, "subtask_name": "part"})
    return s


def preparing() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "PREPARE", "subtask_name": "part"})
    return s


def build(tmp_path, state, ftp=None, **over):
    settings = make_settings(tmp_path, **over)
    repo = PrintRepository(connect(settings.db_path), settings)
    monitor = PrintMonitor(settings, StubPrinter(state), StubCamera(),
                           StubAnalyzer(), StubNotifier(),
                           repository=repo, ftp=ftp)
    return monitor, repo


async def test_prepare_state_is_reported_and_does_not_capture(tmp_path):
    monitor, _ = build(tmp_path, preparing())
    assert await monitor.run_once() == "preparing"


async def test_prepare_fetches_slice_info(tmp_path):
    ftp = StubFtp(slice_info())
    monitor, _ = build(tmp_path, preparing(), ftp=ftp)
    for _ in range(6):
        await monitor.run_once()
    assert ftp.calls == ["part"], "exactly one fetch per print"
    assert monitor.slice_info is not None


async def test_no_fetch_is_attempted_while_running(tmp_path):
    ftp = StubFtp(slice_info())
    monitor, _ = build(tmp_path, running(), ftp=ftp)
    for _ in range(4):
        await monitor.run_once()
    assert ftp.calls == [], "FTPS must never be touched during RUNNING"


async def test_print_row_is_inserted_at_start(tmp_path):
    state = running()
    monitor, repo = build(tmp_path, state)
    await monitor.run_once()
    row = repo.get(monitor.session.id)
    assert row is not None
    assert row["outcome"] == "running"
    assert row["file_name"] == "part"


async def test_completed_print_is_finalized_with_material(tmp_path):
    state = preparing()
    ftp = StubFtp(slice_info())
    monitor, repo = build(tmp_path, state, ftp=ftp)
    await monitor.run_once()                       # PREPARE launches fetch
    for _ in range(3):
        await monitor.run_once()
    state.apply_report({"gcode_state": "RUNNING"})
    await monitor.run_once()
    print_id = monitor.session.id
    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100,
                        "layer_num": 100})
    await monitor.run_once()

    row = repo.get(print_id)
    assert row["outcome"] == OUTCOME_COMPLETED
    assert row["planned_grams"] == pytest.approx(340.0)
    assert row["planned_cost"] == pytest.approx(4.42)
    assert row["consumed_grams"] == pytest.approx(340.0)
    assert row["slice_info_source"] == "ftps"
    assert len(repo.filaments(print_id)) == 2


async def test_stopped_print_records_estimated_consumption(tmp_path):
    state = preparing()
    monitor, repo = build(tmp_path, state, ftp=StubFtp(slice_info()))
    await monitor.run_once()
    state.apply_report({"gcode_state": "RUNNING", "mc_percent": 50,
                        "layer_num": 50, "total_layer_num": 100})
    await monitor.run_once()
    print_id = monitor.session.id
    state.apply_report({"gcode_state": "IDLE"})
    await monitor.run_once()

    row = repo.get(print_id)
    assert row["outcome"] == OUTCOME_STOPPED
    assert row["consumed_grams"] == pytest.approx(170.0)
    assert row["consumption_method"] == "layer_fraction"


async def test_post_print_fetch_when_prepare_was_missed(tmp_path):
    state = running()
    ftp = StubFtp(slice_info())
    monitor, repo = build(tmp_path, state, ftp=ftp)
    await monitor.run_once()
    print_id = monitor.session.id
    assert ftp.calls == []
    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100,
                        "layer_num": 100})
    await monitor.run_once()
    assert ftp.calls == ["part"], "the print is over, so fetching is safe"
    assert repo.get(print_id)["planned_grams"] == pytest.approx(340.0)


async def test_unavailable_slice_info_still_records_the_print(tmp_path):
    state = running()
    monitor, repo = build(tmp_path, state, ftp=StubFtp(fail=True))
    await monitor.run_once()
    print_id = monitor.session.id
    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100,
                        "layer_num": 100})
    await monitor.run_once()

    row = repo.get(print_id)
    assert row["outcome"] == OUTCOME_COMPLETED
    assert row["planned_grams"] is None
    assert row["slice_info_source"] == "unavailable"
    assert row["checks"] is not None, "monitoring stats are still captured"


async def test_disabled_switch_attempts_nothing(tmp_path):
    state = preparing()
    ftp = StubFtp(slice_info())
    monitor, repo = build(tmp_path, state, ftp=ftp, enable_slice_fetch=False)
    for _ in range(3):
        await monitor.run_once()
    assert ftp.calls == []


async def test_monitor_works_without_a_repository(tmp_path):
    settings = make_settings(tmp_path)
    state = running()
    monitor = PrintMonitor(settings, StubPrinter(state), StubCamera(),
                           StubAnalyzer(), StubNotifier())
    assert await monitor.run_once() == "warmup"
    state.apply_report({"gcode_state": "FINISH"})
    assert await monitor.run_once() == "idle", "db is optional"


async def test_new_print_refetches_slice_info(tmp_path):
    state = preparing()
    ftp = StubFtp(slice_info())
    monitor, _ = build(tmp_path, state, ftp=ftp)
    await monitor.run_once()
    state.apply_report({"gcode_state": "RUNNING"})
    await monitor.run_once()
    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100})
    await monitor.run_once()

    state.apply_report({"gcode_state": "PREPARE", "subtask_name": "second"})
    await monitor.run_once()
    assert ftp.calls[-1] == "second", "a new print needs its own slice info"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/detection/test_monitor_db.py -q`
Expected: FAIL, `PrintMonitor.__init__() got an unexpected keyword argument
'repository'`

- [ ] **Step 3: Modify app/detection/monitor.py**

Imports:

```python
from app.bambu.models import PAUSE, PREPARE, PrinterState
from app.bambu.slice_info import SliceInfo
from app.storage.prints import FilamentRow
```

`__init__` additions:

```python
        repository=None,
        ftp=None,
```

```python
        self.repository = repository
        self.ftp = ftp
        self.slice_info: SliceInfo | None = None
        self._slice_source = "unavailable"
        self._slice_task = None
        self._slice_attempted_for: str | None = None
```

`run_once`, with the fetch tick first so it can cancel on any state change:

```python
    async def run_once(self) -> str:
        state = self.state
        await self._tick_slice_fetch(state)

        if state.state == PREPARE:
            # Nothing is extruding yet. This is the only window in which the
            # sliced file is fetched while a print is pending.
            return "preparing"

        if state.state == PAUSE:
            return "paused"

        if not state.is_printing:
            await self._end_session(state.state)
            return "idle"
        ...
```

The fetch lifecycle:

```python
    async def _tick_slice_fetch(self, state: PrinterState) -> None:
        """Advance the PREPARE-window fetch: launch, collect, or cancel.

        Called on every pass so a fetch still in flight when the printer
        leaves PREPARE is cancelled rather than running into a live print.
        """
        if self.ftp is None or self.slice_info is not None:
            return
        if not self.settings.enable_slice_fetch:
            self._slice_source = "disabled"
            return

        in_prepare = state.state == PREPARE

        if self._slice_task is not None and not in_prepare:
            self._slice_task.cancel()
            self._slice_task = None
            self._slice_attempted_for = None
            logger.info("cancelled slice fetch: printer left PREPARE")
            return

        if self._slice_task is None:
            if not in_prepare or not state.file_name:
                return
            if self._slice_attempted_for == state.file_name:
                return
            self._slice_task = asyncio.create_task(
                self.ftp.fetch_slice_info(state.file_name)
            )
            return

        if self._slice_task.done():
            task, self._slice_task = self._slice_task, None
            self._slice_attempted_for = state.file_name
            try:
                self.slice_info = task.result()
            except (asyncio.CancelledError, Exception):
                self.slice_info = None
            self._slice_source = "ftps" if self.slice_info else "unavailable"
```

`_start_session` gains the row insert:

```python
        if self.repository is not None:
            self.repository.insert_running(
                self.session.id,
                state.file_name,
                self.session.started_at,
                str(self.session.directory),
            )
```

`_end_session` becomes async and writes the row:

```python
    async def _end_session(self, final_state: str | None) -> None:
        if self.session is None:
            self._reset_slice_state()
            return

        session, self.session = self.session, None
        state = self.state

        # The print is over, so a fetch here cannot contend with printing.
        if self.slice_info is None and self.ftp is not None:
            if self.settings.enable_slice_fetch and session.file_name:
                self.slice_info = await self.ftp.fetch_slice_info(session.file_name)
                self._slice_source = "ftps" if self.slice_info else "unavailable"

        logger.info(...)
        session.close(final_state)

        if self.repository is not None:
            self._write_print_row(session, final_state, state)

        self.buffer.clear()
        self.detector.reset()
        self.next_interval = self.settings.normal_interval
        self._reset_slice_state()
```

```python
    def _write_print_row(self, session, final_state, state) -> None:
        info = self.slice_info
        try:
            outcome = self.repository.finalize(
                session.id,
                final_state=final_state,
                ended_at=datetime.now(timezone.utc),
                progress=state.progress,
                layer=state.layer,
                total_layers=state.total_layers,
                planned_grams=info.total_grams if info else None,
                planned_meters=info.total_meters if info else None,
                checks=session.checks,
                alerts=session.alerts,
                input_tokens=session.total_input_tokens,
                output_tokens=session.total_output_tokens,
            )
            self.repository.set_slice_source(session.id, self._slice_source)
            if info:
                self.repository.save_filaments(
                    session.id,
                    [
                        FilamentRow(f.slot, f.filament_type, f.color,
                                    f.used_grams, f.used_meters)
                        for f in info.filaments
                    ],
                )
            logger.info("recorded print %s as %s", session.id, outcome)
        except Exception as exc:
            # History is valuable but never worth ending a session over.
            logger.error("could not record print history: %s", exc)

    def _reset_slice_state(self) -> None:
        if self._slice_task is not None:
            self._slice_task.cancel()
        self._slice_task = None
        self.slice_info = None
        self._slice_attempted_for = None
        self._slice_source = "unavailable"
```

`run_forever`'s `finally` awaits the new coroutine, and its delay map gains
`"preparing"` alongside `"idle"` and `"paused"`.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, including the existing `tests/detection/test_monitor.py`.

- [ ] **Step 5: Commit**

```bash
git add app/detection/monitor.py tests/detection/test_monitor_db.py
git commit -m "Record print history from the monitor loop"
```

---

### Task 9: Discord material lines, wiring, probe, README

Folded into one task because each piece is small and none is independently
rejectable.

**Files:**
- Modify: `app/notifications/discord.py`, `app/main.py`,
  `scripts/probe_printer.py`, `README.md`, `.env.example`
- Test: `tests/notifications/test_discord.py` (extend)

**Interfaces:**
- Produces: `MaterialSummary` dataclass in `app/notifications/discord.py`
  with `planned_grams: float | None`, `consumed_grams: float | None`,
  `consumed_cost: float | None`, `estimated: bool`.
  `send_failure` and `format_message` gain
  `material: MaterialSummary | None = None`.

- [ ] **Step 1: Write the failing test**

```python
# appended to tests/notifications/test_discord.py
from app.notifications.discord import MaterialSummary


def material(**over) -> MaterialSummary:
    base = dict(planned_grams=340.0, consumed_grams=240.0,
                consumed_cost=3.12, estimated=True)
    base.update(over)
    return MaterialSummary(**base)


def test_message_includes_material_when_known():
    text = DiscordNotifier.format_message(analysis(), state(), material())
    assert "240" in text
    assert "340" in text
    assert "3.12" in text


def test_estimated_material_says_so():
    text = DiscordNotifier.format_message(analysis(), state(), material())
    assert "estimated" in text.lower(), (
        "a layer-fraction figure must never look measured"
    )


def test_exact_material_does_not_say_estimated():
    text = DiscordNotifier.format_message(analysis(), state(),
                                          material(estimated=False))
    assert "estimated" not in text.lower()


def test_message_omits_material_when_unknown():
    text = DiscordNotifier.format_message(analysis(), state(), None)
    assert "Material" not in text
    assert "0g" not in text, "unknown must never render as zero"


def test_message_omits_material_when_grams_unknown():
    text = DiscordNotifier.format_message(
        analysis(), state(), material(consumed_grams=None, consumed_cost=None))
    assert "Material" not in text


async def test_send_passes_material_through():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), a_frame(), state(), material()) is True
    body = client.calls[0][1]["data"]["payload_json"]
    assert "240" in body
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/notifications -q`
Expected: FAIL, cannot import `MaterialSummary`

- [ ] **Step 3: Modify app/notifications/discord.py**

```python
@dataclass
class MaterialSummary:
    """Filament figures for an alert. `estimated` is True when consumed grams
    came from a layer fraction rather than a measurement."""

    planned_grams: float | None = None
    consumed_grams: float | None = None
    consumed_cost: float | None = None
    estimated: bool = True

    @property
    def renderable(self) -> bool:
        return self.consumed_grams is not None
```

In `format_message`, after the layer line:

```python
        if material is not None and material.renderable:
            parts = [f"about {material.consumed_grams:.0f}g"]
            if material.planned_grams is not None:
                parts.append(f"of {material.planned_grams:.0f}g planned")
            line = f"Material: {' '.join(parts)}"
            if material.consumed_cost is not None:
                line += f" (about {material.consumed_cost:.2f} USD"
                line += ", estimated)" if material.estimated else ")"
            elif material.estimated:
                line += " (estimated)"
            lines.append(line)
```

`format_message` and `send_failure` both take
`material: MaterialSummary | None = None`, keeping existing callers valid.

- [ ] **Step 4: Modify app/main.py**

```python
def build_monitor() -> PrintMonitor:
    settings = get_settings()
    repository = None
    try:
        repository = PrintRepository(connect(settings.db_path), settings)
        repository.reconcile_stale()
    except Exception as exc:
        # History is a convenience. Monitoring is the job.
        logger.error("print history unavailable: %s", exc)

    return PrintMonitor(
        settings=settings,
        printer=BambuMqttClient(settings),
        camera=P1SCamera(settings),
        analyzer=VisionAnalyzer(settings),
        notifier=DiscordNotifier(settings),
        repository=repository,
        ftp=BambuFtpClient(settings) if settings.enable_slice_fetch else None,
    )
```

and a startup log line reporting cost per gram and whether slice fetching is
enabled.

- [ ] **Step 5: Extend scripts/probe_printer.py**

Add `probe_ftp(settings)` reporting spec section 10's unknowns:

```python
async def probe_ftp(settings) -> bool:
    print("--- FTPS (sliced file) ---")
    if not settings.enable_slice_fetch:
        print("SKIP: ENABLE_SLICE_FETCH is false")
        return True

    client = BambuFtpClient(settings)
    loop = asyncio.get_running_loop()

    # Q1: does implicit TLS on 990 accept bblp plus the access code?
    try:
        ftp = await loop.run_in_executor(None, client._connect)
    except Exception as exc:
        print(f"FAIL: connect/login: {type(exc).__name__}: {exc}")
        print("  Check that LAN mode is on and port 990 is reachable.")
        return False
    print(f"OK: connected, implicit TLS on {settings.bambu_ftp_port}")

    # Q2: which directory holds the sliced file, and what is it called?
    for directory in settings.ftp_dirs:
        try:
            names = await loop.run_in_executor(None, ftp.nlst, directory)
        except Exception as exc:
            print(f"  {directory}: listing failed ({exc})")
            continue
        archives = [n for n in names if n.lower().endswith((".3mf", ".gcode"))]
        print(f"  {directory}: {len(names)} entries, {len(archives)} printable")
        for name in archives[:10]:
            print(f"    {name}")

    # Q6: does the server support REST, enabling ranged reads?
    try:
        await loop.run_in_executor(None, lambda: ftp.sendcmd("REST 0"))
        print("OK: REST supported -> ranged reads are possible (spec Q6)")
    except Exception as exc:
        print(f"NOTE: REST unsupported ({exc}); full-archive fetch only")

    try:
        await loop.run_in_executor(None, ftp.quit)
    except Exception:
        pass
    return True
```

Plus, when the MQTT probe captured a report, print the `ams` block shape
(Q8) and note whether `print_error` or `fail_reason` are present (Q7).

The probe must never write. It issues `nlst`, `retrbinary`, and `REST 0` only.

- [ ] **Step 6: Update .env.example and README.md**

`.env.example` gains the commented block:

```bash
# Filament cost. Defaults are a 1kg spool at 13 USD.
# SPOOL_COST=13.0
# SPOOL_WEIGHT_G=1000

# Sliced-file fetch over FTPS, for weight, length and colours.
# Read-only, and never attempted while the printer is RUNNING.
# ENABLE_SLICE_FETCH=true
# BAMBU_FTP_PORT=990
# FTP_TIMEOUT_SECONDS=30
# FTP_MAX_FETCH_BYTES=33554432
# FTP_SEARCH_DIRS=/,/cache
```

README gains a **Print history** section containing: the two-table schema, the
new settings, the statement that the printer stays read-only and how that is
enforced and tested, the explicit warning that `layer_fraction` consumption is
an estimate biased by geometry, and worked queries:

```sql
-- total spend and material, this month
SELECT COUNT(*) AS prints,
       ROUND(SUM(consumed_grams)) AS grams,
       ROUND(SUM(consumed_cost), 2) AS usd
FROM prints
WHERE started_at >= date('now', 'start of month');

-- what failures actually cost
SELECT file_name, outcome, consumption_method,
       ROUND(consumed_grams) AS grams, ROUND(consumed_cost, 2) AS usd
FROM prints
WHERE outcome IN ('stopped', 'failed')
ORDER BY consumed_cost DESC;

-- filament used by colour
SELECT color, filament_type, ROUND(SUM(used_grams)) AS grams
FROM print_filaments
GROUP BY color, filament_type
ORDER BY grams DESC;
```

- [ ] **Step 7: Verify everything**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass

Run: `.venv/bin/python -m ruff check app tests scripts`
Expected: All checks passed

Run: `env -i PATH="$PATH" .venv/bin/python scripts/probe_printer.py; echo $?`
Expected: config guidance and exit 2, no traceback

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "Add material lines to alerts, wire history, extend probe"
```

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|---|---|
| 2.3 write verbs absent, no RUNNING reads, one fetch, bounded, optional | 1, 6, 8 |
| 2.4 constraint enforced by source scan | 1 |
| 3.1 SQLite, WAL, synchronous FULL, user_version | 2 |
| 3.2 summaries only, detections.jsonl untouched | 4, 8 |
| 3.3 insert at start, reconcile stale | 4, 8, 9 |
| 3.4 AMS multi-colour child table | 5, 7, 4 |
| 4 schema including slot semantics | 2, 5 |
| 5 cost from SPOOL_COST / SPOOL_WEIGHT_G, snapshotted | 1, 4 |
| 6 consumption, method labelling, estimate honesty | 3, 4, 9 |
| 7.1 FTPS client, implicit TLS, executor | 6 |
| 7.2 fetch lifecycle with cancel on state change | 8 |
| 7.3 outcome table | 3 |
| 8 Discord material lines with "estimated" | 9 |
| 9 configuration additions | 1, 9 |
| 10 eight probe unknowns | 9 |
| 11 testing requirements | every task |
| 12 out of scope | no tasks, correct |

No gaps. Spec section 6's deferred gcode-exact path is correctly absent.

**2. Placeholder scan**

No TBD or TODO. Task 9 step 6 enumerates README content rather than quoting
prose, but the SQL and the env block are given literally, so nothing is left
to invention.

**3. Type consistency**

- `FilamentUsage` (Task 5, parser) and `FilamentRow` (Task 4, db) are distinct
  by design; Task 8 converts one to the other explicitly. Both use `slot`,
  `filament_type`, `color`, `used_grams`, `used_meters`.
- `determine_outcome` / `compute_consumption` signatures in Task 3 match the
  Task 4 call in `finalize`.
- `SliceInfo.total_grams` and `.total_meters` are properties returning
  `float | None`; Task 8 passes them to `planned_grams` / `planned_meters`,
  which are `float | None`. Consistent.
- `AmsSlot` (Task 7) is used for live state only and never written to
  `print_filaments`; sliced figures are authoritative for recorded usage. This
  is intentional, not an omission.
- `normalize_color` lives in `slice_info.py` and is imported by `ams.py`, so
  colour formatting is identical on both paths.
- `PrintMonitor._end_session` becomes async in Task 8; the only callers are
  `run_once` and `run_forever`, both async. Verified against the Task 8 diff.
- `MaterialSummary` (Task 9) is constructed by the notifier's caller. Task 8's
  monitor does not yet build one at alert time; the alert enrichment is wired
  in Task 9 step 4 by passing `self.slice_info`-derived figures. Task 9 step 3
  keeps the parameter optional so Task 8 remains valid in isolation.
