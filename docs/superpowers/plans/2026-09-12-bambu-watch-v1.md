# bambu-watch V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An advisory Raspberry Pi service that samples a Bambu Lab P1S chamber camera while printing, classifies failures with Claude, requires two matching detections, and posts a Discord alert with the image.

**Architecture:** A single asyncio process. A paho-mqtt client maintains a normalized `PrinterState` from the printer's incremental reports. When state is RUNNING, a monitor loop captures JPEG frames on an interval into a 3-frame rolling buffer, sends them to Claude for structured classification, and feeds the result to a pure-logic detector state machine. The detector decides the next capture interval and, on a confirmed failure, calls the Discord notifier. Session frames and detections persist to disk for later evaluation.

**Tech Stack:** Python 3.12, asyncio, paho-mqtt 2.1, anthropic 1.5 (`messages.parse` structured output), pydantic 2.13 + pydantic-settings, Pillow, httpx2, pytest + pytest-asyncio, Docker.

**Spec:** `docs/superpowers/specs/2026-09-12-bambu-watch-design.md`

## Global Constraints

- Python 3.12. Managed with `uv`; venv at `.venv`.
- Vision model default `claude-sonnet-5`. Never use `budget_tokens` (400 on this model); thinking is `{"type": "adaptive"}`.
- Vision effort default `low`, passed as `output_config={"effort": "low"}`.
- Frames downscaled to width 640 before upload. Native capture resolution is preserved on disk.
- `ParsedMessage.parsed_output` is `Optional`. A `None` value or `stop_reason == "refusal"` is an inconclusive check, never a failure.
- The service never controls or pauses the printer. No MQTT publish other than `pushall`.
- `.env` is gitignored and populated by the user. No credential value is ever committed or written by tooling.
- All source is ASCII.
- Detection defaults: normal interval 45s, suspicious interval 10s, suspicion threshold 0.80, confirmation threshold 0.85, 2 total matching detections, suspicion max checks 6, alert cooldown 15 min, frame history 3.
- No test may spend API credit or require printer hardware.

---

## File Structure

| Path | Responsibility |
|---|---|
| `app/config.py` | `Settings` via pydantic-settings; every tunable in one place |
| `app/bambu/models.py` | `PrinterState`, `ImageFrame`, report-to-state normalization |
| `app/bambu/mqtt_client.py` | MQTT/TLS connection, reconnect, `pushall`, state updates |
| `app/bambu/camera.py` | `Camera` protocol + `P1SCamera` TLS/port-6000 implementation |
| `app/vision/schemas.py` | `FailureAnalysis` pydantic schema |
| `app/vision/prompts.py` | System prompt and per-request metadata rendering |
| `app/vision/analyzer.py` | Claude client wrapper, image downscaling, usage reporting |
| `app/detection/history.py` | `SnapshotBuffer` rolling frame buffer |
| `app/detection/confirmation.py` | `FailureDetector` pure-logic state machine |
| `app/detection/monitor.py` | `PrintMonitor` loop; owns session lifecycle |
| `app/notifications/discord.py` | Webhook POST with embed and image attachment |
| `app/storage/session.py` | `PrintSession` directory layout, frame and detection writes |
| `app/main.py` | Wiring and entrypoint |
| `scripts/probe_printer.py` | Throwaway hardware validation; writes real fixtures |

---

### Task 1: Project scaffold and configuration

**Files:**
- Create: `pyproject.toml`, `.env.example`, `app/__init__.py`, `app/config.py`
- Create: `tests/__init__.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `app.config.Settings` (pydantic-settings `BaseSettings`) with fields
  `bambu_host: str`, `bambu_serial: str`, `bambu_access_code: str`,
  `anthropic_api_key: str`, `discord_webhook_url: str`,
  `vision_model: str = "claude-sonnet-5"`, `vision_effort: str = "low"`,
  `frame_upload_width: int = 640`, `normal_interval: int = 45`,
  `suspicious_interval: int = 10`, `suspicion_threshold: float = 0.80`,
  `confirmation_threshold: float = 0.85`, `suspicion_max_checks: int = 6`,
  `alert_cooldown_minutes: int = 15`, `frame_history: int = 3`,
  `data_dir: Path = Path("data")`, `save_frames: bool = True`,
  `max_session_frames: int = 2000`.
  Also `get_settings() -> Settings` (module-level cached accessor).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
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


def test_missing_required_secret_raises(monkeypatch):
    for k in REQUIRED:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("app.config.Settings.model_config", {"env_file": None}, raising=False)
    with pytest.raises(Exception):
        Settings()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Write pyproject.toml**

```toml
[project]
name = "bambu-watch"
version = "0.1.0"
description = "Advisory AI print-failure monitor for the Bambu Lab P1S"
requires-python = ">=3.12"
dependencies = [
    "anthropic>=1.5.0",
    "paho-mqtt>=2.1.0",
    "pydantic>=2.13.0",
    "pydantic-settings>=2.15.0",
    "pillow>=12.0.0",
    "httpx2>=2.12.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "ruff>=0.6"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
target-version = "py312"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]
```

- [ ] **Step 4: Write app/config.py**

```python
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
```

- [ ] **Step 5: Write .env.example**

```bash
# Printer, on your LAN. Access code is in the P1S screen under
# Settings > WLAN. Never commit the real .env.
BAMBU_HOST=192.168.1.123
BAMBU_SERIAL=01Pxxxxxxxxxxxx
BAMBU_ACCESS_CODE=xxxxxxxx

# Vision API
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx

# Discord webhook (Server Settings > Integrations > Webhooks)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy

# Optional tuning. Defaults shown; raise NORMAL_INTERVAL to cut API cost.
# VISION_MODEL=claude-sonnet-5
# VISION_EFFORT=low
# FRAME_UPLOAD_WIDTH=640
# NORMAL_INTERVAL=45
# SUSPICIOUS_INTERVAL=10
# SUSPICION_THRESHOLD=0.80
# CONFIRMATION_THRESHOLD=0.85
# ALERT_COOLDOWN_MINUTES=15
```

- [ ] **Step 6: Install dev deps and run tests**

Run: `uv pip install -e ".[dev]" && .venv/bin/python -m pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .env.example app tests
git commit -m "Add project scaffold and configuration

Every detection and cost tunable is a settings field so sensitivity
and API spend can be adjusted without a code change."
```

---

### Task 2: Printer state model and report normalization

**Files:**
- Create: `app/bambu/__init__.py`, `app/bambu/models.py`
- Create: `tests/bambu/__init__.py`, `tests/bambu/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PrinterState` dataclass: `printing: bool = False`, `state: str | None = None`,
    `progress: int | None = None`, `layer: int | None = None`,
    `total_layers: int | None = None`, `remaining_minutes: int | None = None`,
    `file_name: str | None = None`, plus method
    `apply_report(self, print_data: dict) -> None` and property `is_printing -> bool`.
  - `ImageFrame` dataclass: `timestamp: datetime`, `jpeg: bytes`.
  - Module constants `RUNNING = "RUNNING"`, `PAUSE = "PAUSE"`, `FINISH = "FINISH"`,
    `FAILED = "FAILED"`, `IDLE = "IDLE"`, `PREPARE = "PREPARE"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/bambu/test_models.py
from datetime import datetime, timezone

from app.bambu.models import ImageFrame, PrinterState


def test_full_report_populates_state():
    s = PrinterState()
    s.apply_report({
        "gcode_state": "RUNNING",
        "mc_percent": 67,
        "layer_num": 845,
        "total_layer_num": 1261,
        "mc_remaining_time": 92,
        "subtask_name": "Mask_Final_v17",
        "gcode_file": "Metadata/plate_1.gcode",
    })
    assert s.state == "RUNNING"
    assert s.printing is True
    assert s.progress == 67
    assert s.layer == 845
    assert s.total_layers == 1261
    assert s.remaining_minutes == 92
    assert s.file_name == "Mask_Final_v17"


def test_incremental_report_preserves_absent_fields():
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 10, "layer_num": 5,
                    "subtask_name": "thing"})
    s.apply_report({"layer_num": 6})
    assert s.layer == 6
    assert s.progress == 10, "absent field must not be cleared"
    assert s.state == "RUNNING"
    assert s.file_name == "thing"


def test_subtask_name_preferred_over_gcode_file():
    s = PrinterState()
    s.apply_report({"gcode_file": "Metadata/plate_1.gcode"})
    assert s.file_name == "Metadata/plate_1.gcode", "falls back when no subtask_name"
    s.apply_report({"subtask_name": "Real_Name"})
    assert s.file_name == "Real_Name"


def test_blank_subtask_name_does_not_overwrite():
    s = PrinterState()
    s.apply_report({"subtask_name": "Real_Name"})
    s.apply_report({"subtask_name": ""})
    assert s.file_name == "Real_Name"


def test_printing_flag_tracks_gcode_state():
    s = PrinterState()
    for state, expected in [("RUNNING", True), ("PAUSE", False), ("FINISH", False),
                            ("FAILED", False), ("IDLE", False), ("PREPARE", False)]:
        s.apply_report({"gcode_state": state})
        assert s.printing is expected, state


def test_image_frame_holds_bytes():
    f = ImageFrame(timestamp=datetime.now(timezone.utc), jpeg=b"\xff\xd8\xff\xd9")
    assert f.jpeg.startswith(b"\xff\xd8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/bambu/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.bambu'`

- [ ] **Step 3: Write app/bambu/models.py**

```python
"""Normalized printer state. Raw Bambu MQTT payloads never leave this module."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

RUNNING = "RUNNING"
PAUSE = "PAUSE"
FINISH = "FINISH"
FAILED = "FAILED"
IDLE = "IDLE"
PREPARE = "PREPARE"

TERMINAL_STATES = frozenset({FINISH, FAILED, IDLE})


@dataclass
class ImageFrame:
    timestamp: datetime
    jpeg: bytes


@dataclass
class PrinterState:
    printing: bool = False
    state: str | None = None
    progress: int | None = None
    layer: int | None = None
    total_layers: int | None = None
    remaining_minutes: int | None = None
    file_name: str | None = None

    def apply_report(self, print_data: dict) -> None:
        """Merge one MQTT `print` object. Reports are incremental, so only
        fields actually present are written."""
        if "gcode_state" in print_data:
            self.state = print_data["gcode_state"]
            self.printing = self.state == RUNNING
        if "mc_percent" in print_data:
            self.progress = _as_int(print_data["mc_percent"])
        if "layer_num" in print_data:
            self.layer = _as_int(print_data["layer_num"])
        if "total_layer_num" in print_data:
            self.total_layers = _as_int(print_data["total_layer_num"])
        if "mc_remaining_time" in print_data:
            self.remaining_minutes = _as_int(print_data["mc_remaining_time"])

        # subtask_name is the human-readable print name. gcode_file reads as
        # "Metadata/plate_1.gcode" and is only a fallback.
        name = print_data.get("subtask_name")
        if name:
            self.file_name = name
        elif not self.file_name and print_data.get("gcode_file"):
            self.file_name = print_data["gcode_file"]

    @property
    def is_printing(self) -> bool:
        return self.state == RUNNING


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/bambu/test_models.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/bambu tests/bambu
git commit -m "Add normalized printer state model

Reports from the P1S are incremental, so apply_report writes only
keys actually present rather than overwriting absent fields with
None. Prefers subtask_name over gcode_file, which reads as
Metadata/plate_1.gcode and is useless in an alert."
```

---

### Task 3: Failure detector state machine

This is the highest-value task. The state machine is pure logic with no I/O,
and it is where the source brief's four defects lived. Each defect gets a
named regression test.

**Files:**
- Create: `app/detection/__init__.py`, `app/detection/confirmation.py`
- Create: `app/vision/__init__.py`, `app/vision/schemas.py`
- Create: `tests/detection/__init__.py`, `tests/detection/test_confirmation.py`

**Interfaces:**
- Consumes: `app.config.Settings` (Task 1).
- Produces:
  - `app.vision.schemas.FailureAnalysis` pydantic model: `status: Literal["healthy","uncertain","failure"]`,
    `confidence: float`, `failure_type: Literal["none","spaghetti","detached_print","detached_support","layer_shift","midair_printing","warping","nozzle_blob","unknown"]`,
    `severity: Literal["none","low","medium","high"]`, `explanation: str`.
  - `app.detection.confirmation.Decision` dataclass: `alert: bool`, `next_interval: int`, `reason: str`.
  - `app.detection.confirmation.FailureDetector` with
    `__init__(self, settings: Settings, now: Callable[[], datetime] = ...)`,
    `process(self, result: FailureAnalysis | None, session_id: str) -> Decision`,
    and `reset(self) -> None`.

`process` returns a `Decision`; it performs no I/O and sends nothing. The
caller (Task 7 monitor) acts on `decision.alert`. A `None` result means the
analysis was inconclusive (parse returned nothing, or refusal).

- [ ] **Step 1: Write the failing test**

```python
# tests/detection/test_confirmation.py
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.detection.confirmation import FailureDetector
from app.vision.schemas import FailureAnalysis

SESSION = "session-1"


def make_settings(**over) -> Settings:
    base = dict(
        bambu_host="h", bambu_serial="s", bambu_access_code="c",
        anthropic_api_key="k", discord_webhook_url="u",
    )
    base.update(over)
    return Settings(**base)


def analysis(status="failure", confidence=0.9, failure_type="spaghetti",
             severity="high", explanation="x") -> FailureAnalysis:
    return FailureAnalysis(status=status, confidence=confidence,
                           failure_type=failure_type, severity=severity,
                           explanation=explanation)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def detector():
    return FailureDetector(make_settings(), now=Clock())


def test_healthy_result_keeps_normal_interval(detector):
    d = detector.process(analysis(status="healthy", confidence=0.99,
                                  failure_type="none", severity="none"), SESSION)
    assert d.alert is False
    assert d.next_interval == 45


def test_first_high_confidence_failure_does_not_alert(detector):
    d = detector.process(analysis(confidence=0.91), SESSION)
    assert d.alert is False, "one detection must never alert"
    assert d.next_interval == 10, "must switch to suspicious interval"


def test_second_matching_failure_alerts(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    d = detector.process(analysis(confidence=0.94), SESSION)
    assert d.alert is True
    assert d.next_interval == 45, "post-alert returns to normal cadence"


def test_confirmation_below_threshold_does_not_alert(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    d = detector.process(analysis(confidence=0.82), SESSION)
    assert d.alert is False, "0.82 is under the 0.85 confirmation threshold"


def test_healthy_second_check_clears_suspicion(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    d = detector.process(analysis(status="healthy", confidence=0.97,
                                  failure_type="none", severity="none"), SESSION)
    assert d.alert is False
    assert d.next_interval == 45, "cleared suspicion returns to normal interval"


# --- Regression tests, one per defect in the source brief ---

def test_regression_low_confidence_does_not_pin_suspicious_interval(detector):
    """Defect 4.1: `if confidence < 0.80: return` left pending state set,
    pinning the detector at the 10s interval for the rest of the print."""
    for _ in range(detector.settings.suspicion_max_checks + 2):
        d = detector.process(analysis(status="uncertain", confidence=0.4,
                                      failure_type="unknown", severity="low"), SESSION)
    assert d.next_interval == 45, "suspicion must not persist indefinitely"
    assert d.alert is False


def test_regression_disagreeing_failure_type_replaces_candidate(detector):
    """Defect 4.2: a differing failure_type fell through, leaving the stale
    first candidate pending forever."""
    detector.process(analysis(confidence=0.91, failure_type="spaghetti"), SESSION)
    d = detector.process(analysis(confidence=0.93, failure_type="layer_shift"), SESSION)
    assert d.alert is False, "disagreement must not alert"
    assert d.next_interval == 10, "stays suspicious"
    # The new type is now the pending candidate, so it can confirm itself.
    d = detector.process(analysis(confidence=0.95, failure_type="layer_shift"), SESSION)
    assert d.alert is True, "new candidate must be confirmable"


def test_regression_suspicion_times_out(detector):
    """Defect 4.3: nothing ever abandoned an unresolved suspicion."""
    detector.process(analysis(confidence=0.91, failure_type="spaghetti"), SESSION)
    last = None
    for _ in range(detector.settings.suspicion_max_checks + 1):
        last = detector.process(analysis(status="uncertain", confidence=0.5,
                                         failure_type="unknown", severity="low"), SESSION)
    assert last.next_interval == 45
    assert last.alert is False


def test_inconclusive_none_result_is_not_a_failure(detector):
    d = detector.process(None, SESSION)
    assert d.alert is False
    assert d.next_interval == 45


def test_cooldown_suppresses_same_failure_type():
    clock = Clock()
    det = FailureDetector(make_settings(), now=clock)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is True
    clock.advance(minutes=1)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is False, "within cooldown"


def test_cooldown_expires():
    clock = Clock()
    det = FailureDetector(make_settings(), now=clock)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is True
    clock.advance(minutes=16)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is True, "cooldown expired"


def test_different_failure_type_alerts_during_cooldown():
    clock = Clock()
    det = FailureDetector(make_settings(), now=clock)
    det.process(analysis(confidence=0.91, failure_type="spaghetti"), SESSION)
    assert det.process(analysis(confidence=0.94, failure_type="spaghetti"), SESSION).alert
    clock.advance(minutes=2)
    det.process(analysis(confidence=0.92, failure_type="detached_print"), SESSION)
    d = det.process(analysis(confidence=0.93, failure_type="detached_print"), SESSION)
    assert d.alert is True, "cooldown is fingerprinted per failure type"


def test_cooldown_is_scoped_per_session():
    clock = Clock()
    det = FailureDetector(make_settings(), now=clock)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is True
    det.reset()
    det.process(analysis(confidence=0.91), "session-2")
    assert det.process(analysis(confidence=0.94), "session-2").alert is True


def test_reset_clears_all_state(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    detector.reset()
    d = detector.process(analysis(status="healthy", confidence=0.99,
                                  failure_type="none", severity="none"), SESSION)
    assert d.next_interval == 45
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/detection/test_confirmation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.detection'`

- [ ] **Step 3: Write app/vision/schemas.py**

```python
"""Structured output schema. The vision model is constrained to this shape,
so no prose parsing is ever required."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FailureType = Literal[
    "none",
    "spaghetti",
    "detached_print",
    "detached_support",
    "layer_shift",
    "midair_printing",
    "warping",
    "nozzle_blob",
    "unknown",
]


class FailureAnalysis(BaseModel):
    status: Literal["healthy", "uncertain", "failure"] = Field(
        description="healthy if the print looks fine, failure only when "
        "continuing would reasonably waste material or ruin quality, "
        "uncertain when the evidence is ambiguous."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    failure_type: FailureType = Field(description="none when status is healthy.")
    severity: Literal["none", "low", "medium", "high"]
    explanation: str = Field(
        description="One or two sentences citing the visual evidence, "
        "including any change between the supplied images."
    )
```

- [ ] **Step 4: Write app/detection/confirmation.py**

```python
"""Failure confirmation state machine.

Pure logic: no I/O, no network, no clock reads except through the injected
`now` callable. A single model response never produces an alert; a second
matching high-confidence detection is required.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.vision.schemas import FailureAnalysis

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Decision:
    """What the monitor should do with one analysis result."""

    alert: bool
    next_interval: int
    reason: str
    analysis: FailureAnalysis | None = None


class FailureDetector:
    def __init__(self, settings: Settings, now: Callable[[], datetime] = _utcnow):
        self.settings = settings
        self._now = now
        self._pending: FailureAnalysis | None = None
        self._suspicious_checks = 0
        self._cooldowns: dict[tuple[str, str], datetime] = {}

    def reset(self) -> None:
        """Called when a print session ends. Cooldowns are fingerprinted by
        session id, but clearing them keeps the dict from growing forever."""
        self._pending = None
        self._suspicious_checks = 0
        self._cooldowns.clear()

    def process(self, result: FailureAnalysis | None, session_id: str) -> Decision:
        # An inconclusive check (no parsed output, or a refusal) carries no
        # information. It must not confirm, and must not extend suspicion.
        if result is None:
            return self._decide_no_suspicion("analysis inconclusive")

        if result.status == "healthy":
            return self._decide_no_suspicion("healthy")

        if result.confidence < self.settings.suspicion_threshold:
            # Below the suspicion bar. If a candidate is already pending this
            # counts against its budget; otherwise nothing happens. The source
            # brief returned here without touching state, which pinned the
            # detector at the suspicious interval for the rest of the print.
            return self._tick_suspicion("below suspicion threshold")

        if self._pending is None:
            self._pending = result
            self._suspicious_checks = 1
            return Decision(
                alert=False,
                next_interval=self.settings.suspicious_interval,
                reason=f"suspicious: {result.failure_type} at {result.confidence:.2f}",
                analysis=result,
            )

        if result.failure_type != self._pending.failure_type:
            # Disagreement. The brief fell through here, stranding the old
            # candidate forever. Replace it and restart confirmation.
            logger.info(
                "failure type changed %s -> %s, restarting confirmation",
                self._pending.failure_type,
                result.failure_type,
            )
            self._pending = result
            self._suspicious_checks = 1
            return Decision(
                alert=False,
                next_interval=self.settings.suspicious_interval,
                reason=f"candidate replaced by {result.failure_type}",
                analysis=result,
            )

        if result.confidence < self.settings.confirmation_threshold:
            return self._tick_suspicion("confirmation below threshold")

        # Two matching detections at or above the confirmation threshold.
        self._pending = None
        self._suspicious_checks = 0

        key = (session_id, result.failure_type)
        last = self._cooldowns.get(key)
        now = self._now()
        cooldown = timedelta(minutes=self.settings.alert_cooldown_minutes)
        if last is not None and now - last < cooldown:
            return Decision(
                alert=False,
                next_interval=self.settings.normal_interval,
                reason=f"confirmed {result.failure_type} suppressed by cooldown",
                analysis=result,
            )

        self._cooldowns[key] = now
        return Decision(
            alert=True,
            next_interval=self.settings.normal_interval,
            reason=f"confirmed {result.failure_type} at {result.confidence:.2f}",
            analysis=result,
        )

    def _tick_suspicion(self, reason: str) -> Decision:
        """Advance an unresolved suspicion, abandoning it once the budget is
        spent so the detector cannot stay at the fast interval forever."""
        if self._pending is None:
            return Decision(
                alert=False,
                next_interval=self.settings.normal_interval,
                reason=reason,
            )

        self._suspicious_checks += 1
        if self._suspicious_checks >= self.settings.suspicion_max_checks:
            logger.info("suspicion of %s timed out", self._pending.failure_type)
            self._pending = None
            self._suspicious_checks = 0
            return Decision(
                alert=False,
                next_interval=self.settings.normal_interval,
                reason=f"{reason}; suspicion timed out",
            )

        return Decision(
            alert=False,
            next_interval=self.settings.suspicious_interval,
            reason=reason,
        )

    def _decide_no_suspicion(self, reason: str) -> Decision:
        self._pending = None
        self._suspicious_checks = 0
        return Decision(
            alert=False, next_interval=self.settings.normal_interval, reason=reason
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/detection/test_confirmation.py -v`
Expected: 15 passed

- [ ] **Step 6: Commit**

```bash
git add app/detection app/vision tests/detection
git commit -m "Add failure confirmation state machine

Requires two matching detections at or above the confirmation
threshold before alerting, with cooldown fingerprinted by session
and failure type.

Fixes three defects in the source brief's sketch: a low-confidence
result no longer leaves pending state set (which pinned the fast
interval for the rest of a print), a disagreeing failure type now
replaces the stale candidate instead of falling through, and an
unresolved suspicion is abandoned after a bounded number of checks.
An inconclusive analysis is treated as carrying no information."
```

---

### Task 4: Vision analyzer

**Files:**
- Create: `app/vision/prompts.py`, `app/vision/analyzer.py`
- Create: `tests/vision/__init__.py`, `tests/vision/test_analyzer.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `PrinterState`/`ImageFrame` (Task 2), `FailureAnalysis` (Task 3).
- Produces:
  - `app.vision.prompts.SYSTEM_PROMPT: str`
  - `app.vision.prompts.render_context(state: PrinterState, count: int, interval: int) -> str`
  - `app.vision.analyzer.AnalysisResult` dataclass: `analysis: FailureAnalysis | None`,
    `input_tokens: int`, `output_tokens: int`, `model: str`, `stop_reason: str | None`.
  - `app.vision.analyzer.downscale_jpeg(jpeg: bytes, width: int) -> bytes`
  - `app.vision.analyzer.VisionAnalyzer` with
    `__init__(self, settings: Settings, client: AsyncAnthropic | None = None)` and
    `async analyze(self, frames: list[ImageFrame], state: PrinterState, interval: int) -> AnalysisResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vision/test_analyzer.py
import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.vision.analyzer import AnalysisResult, VisionAnalyzer, downscale_jpeg
from app.vision.schemas import FailureAnalysis


def make_settings(**over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u")
    base.update(over)
    return Settings(**base)


def jpeg_of(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def frames(n=3) -> list[ImageFrame]:
    t0 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)
    return [ImageFrame(timestamp=t0 + timedelta(seconds=45 * i), jpeg=jpeg_of(1280, 720))
            for i in range(n)]


def state() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 67, "layer_num": 845,
                    "total_layer_num": 1261, "subtask_name": "Mask_Final_v17"})
    return s


# --- downscaling ---

def test_downscale_reduces_width_and_keeps_aspect():
    out = downscale_jpeg(jpeg_of(1280, 720), 640)
    img = Image.open(io.BytesIO(out))
    assert img.size == (640, 360)


def test_downscale_leaves_smaller_image_alone():
    src = jpeg_of(320, 180)
    out = downscale_jpeg(src, 640)
    assert Image.open(io.BytesIO(out)).size == (320, 180)


def test_downscale_returns_original_on_undecodable_bytes():
    assert downscale_jpeg(b"not a jpeg", 640) == b"not a jpeg"


# --- prompt context ---

def test_context_includes_printer_metadata():
    from app.vision.prompts import render_context
    text = render_context(state(), count=3, interval=45)
    assert "67%" in text
    assert "845" in text
    assert "1261" in text
    assert "Image 3" in text


# --- analyzer, with a stubbed client so no test spends credit ---

class StubMessages:
    def __init__(self, parsed, stop_reason="end_turn", usage=(4200, 300)):
        self.parsed = parsed
        self.stop_reason = stop_reason
        self.usage = usage
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)

        class Usage:
            input_tokens, output_tokens = self.usage

        class Resp:
            parsed_output = self.parsed
            stop_reason = self.stop_reason
            usage = Usage()

        return Resp()


class StubClient:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.messages = StubMessages(parsed, stop_reason)


@pytest.fixture
def healthy() -> FailureAnalysis:
    return FailureAnalysis(status="healthy", confidence=0.97, failure_type="none",
                           severity="none", explanation="Looks fine.")


async def test_analyze_returns_parsed_analysis_and_usage(healthy):
    client = StubClient(healthy)
    a = VisionAnalyzer(make_settings(), client=client)
    res = await a.analyze(frames(), state(), interval=45)
    assert isinstance(res, AnalysisResult)
    assert res.analysis is healthy
    assert res.input_tokens == 4200
    assert res.output_tokens == 300
    assert res.model == "claude-sonnet-5"


async def test_analyze_sends_one_image_block_per_frame(healthy):
    client = StubClient(healthy)
    a = VisionAnalyzer(make_settings(), client=client)
    await a.analyze(frames(3), state(), interval=45)
    content = client.messages.calls[0]["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 3
    assert images[0]["source"]["media_type"] == "image/jpeg"


async def test_analyze_requests_adaptive_thinking_and_low_effort(healthy):
    client = StubClient(healthy)
    a = VisionAnalyzer(make_settings(), client=client)
    await a.analyze(frames(), state(), interval=45)
    kwargs = client.messages.calls[0]
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["output_format"] is FailureAnalysis
    assert "budget_tokens" not in str(kwargs), "rejected with 400 on this model"


async def test_analyze_treats_refusal_as_inconclusive(healthy):
    client = StubClient(healthy, stop_reason="refusal")
    a = VisionAnalyzer(make_settings(), client=client)
    res = await a.analyze(frames(), state(), interval=45)
    assert res.analysis is None, "a refusal must never read as a detection"
    assert res.stop_reason == "refusal"


async def test_analyze_handles_none_parsed_output():
    client = StubClient(None)
    a = VisionAnalyzer(make_settings(), client=client)
    res = await a.analyze(frames(), state(), interval=45)
    assert res.analysis is None


async def test_analyze_returns_inconclusive_on_api_error(healthy):
    class Boom:
        class messages:
            @staticmethod
            async def parse(**kwargs):
                raise RuntimeError("connection reset")

    a = VisionAnalyzer(make_settings(), client=Boom())
    res = await a.analyze(frames(), state(), interval=45)
    assert res.analysis is None, "an API error is a skipped check, not a crash"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/vision/test_analyzer.py -v`
Expected: FAIL with `ImportError: cannot import name 'VisionAnalyzer'`

- [ ] **Step 3: Write app/vision/prompts.py**

```python
"""Vision prompts. Deliberately conservative: the cost of a false positive is
a user walking to the printer for nothing, and repeated false positives
destroy trust in the alerts entirely."""

from __future__ import annotations

from app.bambu.models import PrinterState

SYSTEM_PROMPT = """You are a visual monitoring system for an FDM 3D printer.

Your task is only to determine whether the print appears to be failing.

Compare the supplied chronological images.

Look for:
- spaghetti extrusion
- model detachment
- support detachment
- severe warping
- layer shifts
- nozzle blobs
- printing into unsupported open space
- catastrophic extrusion failures

Do not classify normal support structures, infill, travel strings, seams,
bridges, or expected geometry as failures.

When evidence is ambiguous, return uncertain rather than failure.

Use temporal differences between images as evidence.

A failure means continuing the print is reasonably likely to waste material
or significantly damage print quality."""


def render_context(state: PrinterState, count: int, interval: int) -> str:
    """Describe the printer and the image timeline so the model can reason
    about change over time rather than judging a single frame."""
    lines = ["Printer: Bambu Lab P1S"]
    if state.file_name:
        lines.append(f"Print: {state.file_name}")
    if state.progress is not None:
        lines.append(f"Progress: {state.progress}%")
    if state.layer is not None:
        total = state.total_layers
        lines.append(
            f"Current layer: {state.layer}"
            + (f" of {total}" if total else "")
        )
    if state.total_layers is not None:
        lines.append(f"Total layers: {state.total_layers}")

    lines.append("")
    for i in range(count):
        age = (count - 1 - i) * interval
        when = "current" if age == 0 else f"approximately {age} seconds ago"
        lines.append(f"Image {i + 1}: {when}")

    return "\n".join(lines)
```

- [ ] **Step 4: Write app/vision/analyzer.py**

```python
"""Claude vision analyzer.

Frames are downscaled before upload because images are billed at roughly
(width * height) / 750 tokens and a rolling buffer uploads each frame on
three successive checks. Token usage is returned so real cost per print is
measurable rather than estimated.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from PIL import Image

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.vision.prompts import SYSTEM_PROMPT, render_context
from app.vision.schemas import FailureAnalysis

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    analysis: FailureAnalysis | None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str | None = None


def downscale_jpeg(jpeg: bytes, width: int) -> bytes:
    """Shrink to `width` preserving aspect ratio. Returns the input unchanged
    if it is already narrower or cannot be decoded -- an unreadable frame is
    the camera layer's problem, not a reason to fail the check here."""
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.load()
    except Exception:
        logger.warning("frame could not be decoded for downscaling")
        return jpeg

    if img.width <= width:
        return jpeg

    height = max(1, round(img.height * width / img.width))
    resized = img.convert("RGB").resize((width, height), Image.LANCZOS)
    out = io.BytesIO()
    resized.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


class VisionAnalyzer:
    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None):
        self.settings = settings
        self._client = client or AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def analyze(
        self, frames: list[ImageFrame], state: PrinterState, interval: int
    ) -> AnalysisResult:
        content: list[dict] = []
        for frame in frames:
            data = downscale_jpeg(frame.jpeg, self.settings.frame_upload_width)
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(data).decode("ascii"),
                    },
                }
            )
        content.append(
            {"type": "text", "text": render_context(state, len(frames), interval)}
        )

        try:
            response = await self._client.messages.parse(
                model=self.settings.vision_model,
                max_tokens=self.settings.vision_max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_format=FailureAnalysis,
                output_config={"effort": self.settings.vision_effort},
                thinking={"type": "adaptive"},
            )
        except Exception as exc:
            # A failed analysis is a skipped check. The monitor keeps running.
            logger.warning("vision analysis failed: %s", exc)
            return AnalysisResult(analysis=None, model=self.settings.vision_model)

        stop_reason = getattr(response, "stop_reason", None)
        usage = getattr(response, "usage", None)

        analysis = response.parsed_output
        if stop_reason == "refusal":
            # Never let a safety refusal read as a failure detection.
            logger.warning("vision request refused; treating check as inconclusive")
            analysis = None

        return AnalysisResult(
            analysis=analysis,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            model=self.settings.vision_model,
            stop_reason=stop_reason,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/vision/test_analyzer.py -v`
Expected: 10 passed

- [ ] **Step 6: Commit**

```bash
git add app/vision tests/vision
git commit -m "Add Claude vision analyzer with structured output

Constrains the response to the FailureAnalysis schema via
messages.parse, so no prose parsing is needed. Frames are
downscaled to 640px wide before upload because images bill at
roughly (w*h)/750 tokens and the rolling buffer uploads each frame
three times; this roughly halves cost per check.

A refusal stop_reason, a None parsed_output, and an API exception
all resolve to an inconclusive check rather than a detection or a
crash. Token usage is returned so cost per print is measurable."
```

---

### Task 5: Snapshot buffer and session storage

**Files:**
- Create: `app/detection/history.py`, `app/storage/__init__.py`, `app/storage/session.py`
- Create: `tests/detection/test_history.py`, `tests/storage/__init__.py`, `tests/storage/test_session.py`

**Interfaces:**
- Consumes: `ImageFrame` (Task 2), `Settings` (Task 1), `AnalysisResult` (Task 4), `Decision` (Task 3).
- Produces:
  - `app.detection.history.SnapshotBuffer` with `__init__(self, capacity: int)`,
    `add(self, frame: ImageFrame) -> None`, `latest(self, n: int) -> list[ImageFrame]`,
    `ready(self) -> bool`, `clear(self) -> None`, `__len__`.
  - `app.storage.session.PrintSession` with
    `create(cls, settings: Settings, file_name: str | None, started_at: datetime) -> PrintSession`,
    attributes `id: str`, `directory: Path`, `frame_count: int`,
    methods `save_frame(self, frame: ImageFrame) -> Path | None`,
    `record(self, result: AnalysisResult, decision: Decision, frame_path: Path | None) -> None`,
    `close(self, final_state: str | None) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/detection/test_history.py
from datetime import datetime, timedelta, timezone

from app.bambu.models import ImageFrame
from app.detection.history import SnapshotBuffer


def frame(i: int) -> ImageFrame:
    return ImageFrame(
        timestamp=datetime(2026, 9, 12, tzinfo=timezone.utc) + timedelta(seconds=i),
        jpeg=bytes([i]),
    )


def test_buffer_not_ready_until_capacity():
    b = SnapshotBuffer(capacity=3)
    assert b.ready() is False
    b.add(frame(1))
    b.add(frame(2))
    assert b.ready() is False
    b.add(frame(3))
    assert b.ready() is True


def test_buffer_evicts_oldest():
    b = SnapshotBuffer(capacity=3)
    for i in range(1, 6):
        b.add(frame(i))
    assert len(b) == 3
    assert [f.jpeg[0] for f in b.latest(3)] == [3, 4, 5]


def test_latest_is_chronological_oldest_first():
    b = SnapshotBuffer(capacity=3)
    for i in range(1, 4):
        b.add(frame(i))
    got = b.latest(3)
    assert got[0].timestamp < got[-1].timestamp


def test_latest_caps_at_available():
    b = SnapshotBuffer(capacity=3)
    b.add(frame(1))
    assert len(b.latest(3)) == 1


def test_clear_empties_buffer():
    b = SnapshotBuffer(capacity=3)
    b.add(frame(1))
    b.clear()
    assert len(b) == 0
    assert b.ready() is False
```

```python
# tests/storage/test_session.py
import json
from datetime import datetime, timezone

from app.bambu.models import ImageFrame
from app.config import Settings
from app.detection.confirmation import Decision
from app.storage.session import PrintSession
from app.vision.analyzer import AnalysisResult
from app.vision.schemas import FailureAnalysis


def make_settings(tmp_path, **over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u", data_dir=tmp_path)
    base.update(over)
    return Settings(**base)


def a_frame() -> ImageFrame:
    return ImageFrame(timestamp=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
                      jpeg=b"\xff\xd8fake\xff\xd9")


def a_result() -> AnalysisResult:
    return AnalysisResult(
        analysis=FailureAnalysis(status="failure", confidence=0.93,
                                 failure_type="spaghetti", severity="high",
                                 explanation="Loose filament above the part."),
        input_tokens=1550, output_tokens=280, model="claude-sonnet-5",
        stop_reason="end_turn",
    )


def test_create_makes_session_directory(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "Mask_Final_v17",
                            datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    assert s.directory.is_dir()
    assert (s.directory / "frames").is_dir()
    assert "mask_final_v17" in s.directory.name.lower()
    assert json.loads((s.directory / "metadata.json").read_text())["file_name"] == "Mask_Final_v17"


def test_session_name_is_filesystem_safe(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "a/b c:d*?.3mf",
                            datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    for ch in "/:*?":
        assert ch not in s.directory.name


def test_save_frame_writes_numbered_jpeg(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x",
                            datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    p1 = s.save_frame(a_frame())
    p2 = s.save_frame(a_frame())
    assert p1.name == "0001.jpg"
    assert p2.name == "0002.jpg"
    assert p1.read_bytes() == b"\xff\xd8fake\xff\xd9"
    assert s.frame_count == 2


def test_save_frame_respects_disabled_saving(tmp_path):
    s = PrintSession.create(make_settings(tmp_path, save_frames=False), "x",
                            datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    assert s.save_frame(a_frame()) is None


def test_save_frame_stops_at_max_frames(tmp_path):
    s = PrintSession.create(make_settings(tmp_path, max_session_frames=2), "x",
                            datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    assert s.save_frame(a_frame()) is not None
    assert s.save_frame(a_frame()) is not None
    assert s.save_frame(a_frame()) is None, "storage must not grow without bound"


def test_record_appends_jsonl_with_usage(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x",
                            datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    path = s.save_frame(a_frame())
    s.record(a_result(), Decision(alert=True, next_interval=45, reason="confirmed"), path)
    lines = (s.directory / "detections.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["failure_type"] == "spaghetti"
    assert row["confidence"] == 0.93
    assert row["alert"] is True
    assert row["input_tokens"] == 1550
    assert row["output_tokens"] == 280
    assert row["frame"] == "frames/0001.jpg"


def test_record_handles_inconclusive_result(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x",
                            datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    s.record(AnalysisResult(analysis=None, model="claude-sonnet-5"),
             Decision(alert=False, next_interval=45, reason="inconclusive"), None)
    row = json.loads((s.directory / "detections.jsonl").read_text().strip())
    assert row["status"] is None
    assert row["frame"] is None


def test_close_writes_totals(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x",
                            datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc))
    s.record(a_result(), Decision(alert=True, next_interval=45, reason="c"), None)
    s.close("FINISH")
    meta = json.loads((s.directory / "metadata.json").read_text())
    assert meta["final_state"] == "FINISH"
    assert meta["alerts"] == 1
    assert meta["checks"] == 1
    assert meta["total_input_tokens"] == 1550
    assert meta["ended_at"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/detection/test_history.py tests/storage -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.detection.history'`

- [ ] **Step 3: Write app/detection/history.py**

```python
"""Rolling frame buffer. Gives the vision model temporal context instead of
forcing it to judge a print from one image."""

from __future__ import annotations

from collections import deque

from app.bambu.models import ImageFrame


class SnapshotBuffer:
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._frames: deque[ImageFrame] = deque(maxlen=capacity)

    def add(self, frame: ImageFrame) -> None:
        self._frames.append(frame)

    def latest(self, n: int) -> list[ImageFrame]:
        """Oldest first, so the model reads the images as a timeline."""
        if n <= 0:
            return []
        return list(self._frames)[-n:]

    def ready(self) -> bool:
        return len(self._frames) >= self.capacity

    def clear(self) -> None:
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)
```

- [ ] **Step 4: Write app/storage/session.py**

```python
"""Per-print session storage.

Produces a dataset of real prints and real failures, which is what makes
false-positive rate measurable later. Frame saving is capped so a long print
cannot fill the Pi's disk.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.bambu.models import ImageFrame
from app.config import Settings
from app.detection.confirmation import Decision
from app.vision.analyzer import AnalysisResult

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str | None) -> str:
    if not name:
        return "print"
    stem = Path(name).stem
    cleaned = _UNSAFE.sub("_", stem).strip("_")
    return (cleaned or "print")[:48]


@dataclass
class PrintSession:
    id: str
    directory: Path
    started_at: datetime
    file_name: str | None
    settings: Settings
    frame_count: int = 0
    checks: int = 0
    alerts: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    _saved_frames: int = field(default=0, repr=False)

    @classmethod
    def create(
        cls, settings: Settings, file_name: str | None, started_at: datetime
    ) -> PrintSession:
        stamp = started_at.strftime("%Y-%m-%d_%H%M%S")
        directory = settings.sessions_dir / f"{stamp}_{_slug(file_name)}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "frames").mkdir(exist_ok=True)

        session = cls(
            id=str(uuid.uuid4()),
            directory=directory,
            started_at=started_at,
            file_name=file_name,
            settings=settings,
        )
        session._write_metadata(final_state=None, ended_at=None)
        return session

    def save_frame(self, frame: ImageFrame) -> Path | None:
        if not self.settings.save_frames:
            return None
        if self._saved_frames >= self.settings.max_session_frames:
            return None

        self._saved_frames += 1
        self.frame_count = self._saved_frames
        path = self.directory / "frames" / f"{self._saved_frames:04d}.jpg"
        try:
            path.write_bytes(frame.jpeg)
        except OSError as exc:
            logger.warning("could not write frame: %s", exc)
            return None
        return path

    def record(
        self,
        result: AnalysisResult,
        decision: Decision,
        frame_path: Path | None,
    ) -> None:
        analysis = result.analysis
        self.checks += 1
        self.total_input_tokens += result.input_tokens
        self.total_output_tokens += result.output_tokens
        if decision.alert:
            self.alerts += 1

        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.id,
            "status": analysis.status if analysis else None,
            "confidence": analysis.confidence if analysis else None,
            "failure_type": analysis.failure_type if analysis else None,
            "severity": analysis.severity if analysis else None,
            "explanation": analysis.explanation if analysis else None,
            "alert": decision.alert,
            "reason": decision.reason,
            "next_interval": decision.next_interval,
            "model": result.model,
            "stop_reason": result.stop_reason,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "frame": (
                str(frame_path.relative_to(self.directory)) if frame_path else None
            ),
        }
        try:
            with (self.directory / "detections.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError as exc:
            logger.warning("could not append detection: %s", exc)

    def close(self, final_state: str | None) -> None:
        self._write_metadata(
            final_state=final_state, ended_at=datetime.now(timezone.utc)
        )

    def _write_metadata(
        self, final_state: str | None, ended_at: datetime | None
    ) -> None:
        meta = {
            "id": self.id,
            "file_name": self.file_name,
            "started_at": self.started_at.isoformat(),
            "ended_at": ended_at.isoformat() if ended_at else None,
            "final_state": final_state,
            "checks": self.checks,
            "alerts": self.alerts,
            "frames_saved": self.frame_count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "vision_model": self.settings.vision_model,
        }
        try:
            (self.directory / "metadata.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("could not write session metadata: %s", exc)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/detection/test_history.py tests/storage -v`
Expected: 14 passed

- [ ] **Step 6: Commit**

```bash
git add app/detection/history.py app/storage tests/detection/test_history.py tests/storage
git commit -m "Add rolling frame buffer and per-session storage

The buffer returns frames oldest-first so the model reads them as a
timeline. Sessions persist frames, per-check detections with token
usage, and end-of-print totals, which is what makes false-positive
rate and real cost measurable later. Frame writes are capped by
max_session_frames so a long print cannot fill the Pi's disk."
```

---

### Task 6: MQTT client and camera client

Both are network edges against undocumented endpoints. Neither may raise into
the monitor loop.

**Files:**
- Create: `app/bambu/mqtt_client.py`, `app/bambu/camera.py`
- Create: `tests/bambu/test_mqtt_client.py`, `tests/bambu/test_camera.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `PrinterState`/`ImageFrame` (Task 2).
- Produces:
  - `app.bambu.mqtt_client.BambuMqttClient` with `__init__(self, settings: Settings)`,
    attribute `state: PrinterState`, `connect(self) -> None`, `disconnect(self) -> None`,
    `handle_payload(self, payload: bytes) -> None`, `request_pushall(self) -> None`,
    property `connected: bool`.
  - `app.bambu.camera.Camera` (typing.Protocol) with `async capture(self) -> ImageFrame`.
  - `app.bambu.camera.P1SCamera` implementing it, plus
    `build_auth_packet(username: str, access_code: str) -> bytes` and
    `read_frame(reader) -> bytes` helpers exposed for testing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/bambu/test_mqtt_client.py
import json

from app.bambu.mqtt_client import BambuMqttClient
from app.config import Settings


def make_settings(**over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="01P00A", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u")
    base.update(over)
    return Settings(**base)


def test_topics_use_serial():
    c = BambuMqttClient(make_settings())
    assert c.report_topic == "device/01P00A/report"
    assert c.request_topic == "device/01P00A/request"


def test_handle_payload_updates_state():
    c = BambuMqttClient(make_settings())
    c.handle_payload(json.dumps({
        "print": {"gcode_state": "RUNNING", "mc_percent": 42,
                  "subtask_name": "thing"}
    }).encode())
    assert c.state.state == "RUNNING"
    assert c.state.progress == 42
    assert c.state.file_name == "thing"


def test_handle_payload_ignores_non_print_messages():
    c = BambuMqttClient(make_settings())
    c.handle_payload(json.dumps({"info": {"command": "get_version"}}).encode())
    assert c.state.state is None


def test_handle_payload_survives_malformed_json():
    c = BambuMqttClient(make_settings())
    c.handle_payload(b"{not json")
    assert c.state.state is None, "a bad payload must not raise"


def test_handle_payload_survives_unexpected_shape():
    c = BambuMqttClient(make_settings())
    c.handle_payload(json.dumps({"print": "not-a-dict"}).encode())
    assert c.state.state is None


def test_pushall_publishes_to_request_topic():
    c = BambuMqttClient(make_settings())
    published = []

    class FakeClient:
        def publish(self, topic, payload, qos=0):
            published.append((topic, json.loads(payload)))

    c._client = FakeClient()
    c.request_pushall()
    topic, body = published[0]
    assert topic == "device/01P00A/request"
    assert body["pushing"]["command"] == "pushall", (
        "without pushall the printer only sends incremental reports"
    )


def test_pushall_increments_sequence_id():
    c = BambuMqttClient(make_settings())
    seen = []

    class FakeClient:
        def publish(self, topic, payload, qos=0):
            seen.append(json.loads(payload)["pushing"]["sequence_id"])

    c._client = FakeClient()
    c.request_pushall()
    c.request_pushall()
    assert seen[0] != seen[1]
```

```python
# tests/bambu/test_camera.py
import asyncio

import pytest

from app.bambu.camera import build_auth_packet, read_frame

JPEG = b"\xff\xd8" + b"body" + b"\xff\xd9"


def test_auth_packet_is_80_bytes():
    pkt = build_auth_packet("bblp", "abcd1234")
    assert len(pkt) == 80


def test_auth_packet_header_and_fixed_width_fields():
    pkt = build_auth_packet("bblp", "abcd1234")
    assert pkt[:4] == (0x40).to_bytes(4, "little")
    assert pkt[4:8] == (0x3000).to_bytes(4, "little")
    assert pkt[8:12] == b"\x00\x00\x00\x00"
    assert pkt[12:16] == b"\x00\x00\x00\x00"
    assert pkt[16:48].rstrip(b"\x00") == b"bblp"
    assert pkt[48:80].rstrip(b"\x00") == b"abcd1234"


def test_auth_packet_rejects_oversized_credentials():
    with pytest.raises(ValueError):
        build_auth_packet("bblp", "x" * 33)


class FakeReader:
    def __init__(self, data: bytes):
        self._data = data
        self.pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self.pos + n > len(self._data):
            raise asyncio.IncompleteReadError(self._data[self.pos:], n)
        chunk = self._data[self.pos:self.pos + n]
        self.pos += n
        return chunk


def framed(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "little") + b"\x00" * 12 + payload


async def test_read_frame_returns_jpeg_payload():
    got = await read_frame(FakeReader(framed(JPEG)))
    assert got == JPEG


async def test_read_frame_rejects_implausible_length():
    bad = (500_000_000).to_bytes(4, "little") + b"\x00" * 12
    with pytest.raises(ValueError, match="implausible"):
        await read_frame(FakeReader(bad))


async def test_read_frame_rejects_payload_without_jpeg_magic():
    with pytest.raises(ValueError, match="JPEG"):
        await read_frame(FakeReader(framed(b"\x00\x01not a jpeg")))


async def test_read_frame_propagates_truncated_stream():
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(FakeReader(framed(JPEG)[:10]))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/bambu -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.bambu.mqtt_client'`

- [ ] **Step 3: Write app/bambu/mqtt_client.py**

```python
"""MQTT/TLS client for the P1S on the local network.

paho-mqtt runs its own network thread and reconnects on its own; this class
only has to re-subscribe and re-issue pushall on each (re)connect. The
printer's certificate is self-signed, so verification is disabled -- this is
a LAN connection to a device authenticated by its access code.
"""

from __future__ import annotations

import json
import logging
import ssl
from itertools import count

import paho.mqtt.client as mqtt

from app.bambu.models import PrinterState
from app.config import Settings

logger = logging.getLogger(__name__)


class BambuMqttClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = PrinterState()
        self.report_topic = f"device/{settings.bambu_serial}/report"
        self.request_topic = f"device/{settings.bambu_serial}/request"
        self._sequence = count(1)
        self._connected = False
        self._client: mqtt.Client | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-watch-{self.settings.bambu_serial}",
        )
        client.username_pw_set("bblp", self.settings.bambu_access_code)
        client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.tls_insecure_set(True)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        self._client = client
        client.connect_async(self.settings.bambu_host, self.settings.bambu_mqtt_port, 60)
        client.loop_start()

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
        self._connected = False

    def request_pushall(self) -> None:
        """Force a full state snapshot. The P1S otherwise sends only
        incremental reports, so a freshly connected client can sit for
        minutes without learning that a print is already running."""
        if self._client is None:
            return
        payload = json.dumps(
            {"pushing": {"sequence_id": str(next(self._sequence)), "command": "pushall"}}
        )
        self._client.publish(self.request_topic, payload, qos=0)

    def handle_payload(self, payload: bytes) -> None:
        """Parse one report. Never raises: a malformed message from the
        printer must not take down the monitor."""
        try:
            message = json.loads(payload)
        except (ValueError, TypeError):
            logger.debug("ignoring unparseable MQTT payload")
            return

        if not isinstance(message, dict):
            return
        print_data = message.get("print")
        if not isinstance(print_data, dict):
            return

        try:
            self.state.apply_report(print_data)
        except Exception as exc:
            logger.warning("could not apply report: %s", exc)

    # --- paho callbacks (VERSION2 signatures) ---

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logger.error("MQTT connect refused: %s", reason_code)
            return
        self._connected = True
        logger.info("MQTT connected to %s", self.settings.bambu_host)
        client.subscribe(self.report_topic, qos=0)
        self.request_pushall()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = False
        logger.warning("MQTT disconnected: %s (paho will retry)", reason_code)

    def _on_message(self, client, userdata, message):
        self.handle_payload(message.payload)
```

- [ ] **Step 4: Write app/bambu/camera.py**

```python
"""P1S chamber camera.

The protocol on TCP 6000 is undocumented. This implementation targets TLS
with verification disabled, an 80-byte auth payload, then length-prefixed
JPEG frames. The byte layout is derived from open-source implementations and
is the most likely thing in this project to be wrong, which is why the whole
protocol lives behind the `Camera` protocol and is validated by
scripts/probe_printer.py against real hardware.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import datetime, timezone
from typing import Protocol

from app.bambu.models import ImageFrame
from app.config import Settings

logger = logging.getLogger(__name__)

AUTH_PACKET_SIZE = 80
FIELD_SIZE = 32
FRAME_HEADER_SIZE = 16
MAX_FRAME_BYTES = 8 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8"


class Camera(Protocol):
    """Substitutable image source. Nothing downstream knows or cares whether
    frames come from the P1S, a Pi camera, a webcam, or an RTSP stream."""

    async def capture(self) -> ImageFrame: ...


def build_auth_packet(username: str, access_code: str) -> bytes:
    """80 bytes: a 16-byte header then two 32-byte null-padded fields."""
    user = username.encode("ascii")
    code = access_code.encode("ascii")
    if len(user) > FIELD_SIZE or len(code) > FIELD_SIZE:
        raise ValueError(f"username and access code must each be <= {FIELD_SIZE} bytes")

    return b"".join(
        [
            (0x40).to_bytes(4, "little"),
            (0x3000).to_bytes(4, "little"),
            (0x00).to_bytes(4, "little"),
            (0x00).to_bytes(4, "little"),
            user.ljust(FIELD_SIZE, b"\x00"),
            code.ljust(FIELD_SIZE, b"\x00"),
        ]
    )


async def read_frame(reader) -> bytes:
    """Read one length-prefixed JPEG. Raises ValueError if the stream does
    not look like the expected framing, so a desynchronized connection is
    torn down and retried rather than yielding garbage to the model."""
    header = await reader.readexactly(FRAME_HEADER_SIZE)
    length = int.from_bytes(header[:4], "little")
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise ValueError(f"implausible frame length {length}")

    payload = await reader.readexactly(length)
    if not payload.startswith(JPEG_MAGIC):
        raise ValueError("frame payload is not JPEG")
    return payload


class P1SCamera:
    """Opens a connection per capture. The camera stream is continuous and we
    only want one frame every 10-45 seconds, so holding it open would burn
    LAN bandwidth and printer CPU for frames that are discarded."""

    def __init__(self, settings: Settings, timeout: float = 15.0):
        self.settings = settings
        self.timeout = timeout

    async def capture(self) -> ImageFrame:
        jpeg = await asyncio.wait_for(self._capture_once(), timeout=self.timeout)
        return ImageFrame(timestamp=datetime.now(timezone.utc), jpeg=jpeg)

    async def _capture_once(self) -> bytes:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        reader, writer = await asyncio.open_connection(
            self.settings.bambu_host, self.settings.bambu_camera_port, ssl=context
        )
        try:
            writer.write(build_auth_packet("bblp", self.settings.bambu_access_code))
            await writer.drain()
            return await read_frame(reader)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/bambu -v`
Expected: 17 passed

- [ ] **Step 6: Commit**

```bash
git add app/bambu tests/bambu
git commit -m "Add MQTT client and P1S camera client

The MQTT client issues pushall on every connect, without which the
printer sends only incremental reports and a fresh client can sit
for minutes without learning a print is running. handle_payload
never raises so a malformed report cannot take down the monitor.

The camera opens a connection per capture rather than holding the
stream, since only one frame every 10-45s is wanted. The port-6000
byte layout is undocumented and derived from open-source
implementations, so it is isolated behind the Camera protocol and
read_frame rejects anything that does not match the expected
framing rather than passing garbage to the model."
```

---

### Task 7: Discord notifier

**Files:**
- Create: `app/notifications/__init__.py`, `app/notifications/discord.py`
- Create: `tests/notifications/__init__.py`, `tests/notifications/test_discord.py`

**Interfaces:**
- Consumes: `Settings` (Task 1), `PrinterState`/`ImageFrame` (Task 2), `FailureAnalysis` (Task 3).
- Produces: `app.notifications.discord.DiscordNotifier` with
  `__init__(self, settings: Settings, client=None)`,
  `async send_failure(self, analysis: FailureAnalysis, frame: ImageFrame | None, state: PrinterState) -> bool`,
  and `format_message(analysis, state) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/notifications/test_discord.py
from datetime import datetime, timezone

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.notifications.discord import DiscordNotifier
from app.vision.schemas import FailureAnalysis


def make_settings(**over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k",
                discord_webhook_url="https://discord.com/api/webhooks/1/tok")
    base.update(over)
    return Settings(**base)


def analysis() -> FailureAnalysis:
    return FailureAnalysis(status="failure", confidence=0.94,
                           failure_type="detached_support", severity="high",
                           explanation="The front-right support appears separated.")


def state() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 71, "layer_num": 883,
                    "total_layer_num": 1240, "subtask_name": "Mask_Final_v17.3mf"})
    return s


def a_frame() -> ImageFrame:
    return ImageFrame(timestamp=datetime(2026, 9, 12, tzinfo=timezone.utc),
                      jpeg=b"\xff\xd8x\xff\xd9")


class StubResponse:
    def __init__(self, status_code=204):
        self.status_code = status_code
        self.text = ""


class StubClient:
    def __init__(self, status_code=204, boom=False):
        self.status_code = status_code
        self.boom = boom
        self.calls = []

    async def post(self, url, **kwargs):
        if self.boom:
            raise RuntimeError("network down")
        self.calls.append((url, kwargs))
        return StubResponse(self.status_code)


def test_message_contains_actionable_detail():
    text = DiscordNotifier.format_message(analysis(), state())
    assert "detached support" in text.lower()
    assert "94%" in text
    assert "71%" in text
    assert "883" in text
    assert "1240" in text
    assert "Mask_Final_v17.3mf" in text
    assert "Handy" in text, "must tell the user where to act"


def test_message_handles_missing_metadata():
    text = DiscordNotifier.format_message(analysis(), PrinterState())
    assert "detached support" in text.lower()


async def test_send_posts_multipart_with_image():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), a_frame(), state()) is True
    url, kwargs = client.calls[0]
    assert url == "https://discord.com/api/webhooks/1/tok"
    assert "files" in kwargs, "the image is the evidence; it must be attached"
    assert kwargs["files"]["file"][0] == "failure.jpg"


async def test_send_without_frame_posts_json_only():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), None, state()) is True
    _, kwargs = client.calls[0]
    assert "files" not in kwargs
    assert "json" in kwargs


async def test_send_returns_false_on_http_error():
    client = StubClient(status_code=500)
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), a_frame(), state()) is False


async def test_send_returns_false_on_network_exception():
    client = StubClient(boom=True)
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), a_frame(), state()) is False, (
        "a failed notification must not crash the monitor"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/notifications -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.notifications'`

- [ ] **Step 3: Write app/notifications/discord.py**

```python
"""Discord webhook notifier.

The alert has to carry enough for the user to judge the model's reasoning
without walking to the printer, and it has to say where to act. V1 uses a
webhook rather than a bot because nothing needs to be received.
"""

from __future__ import annotations

import json
import logging

import httpx2 as httpx

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.vision.schemas import FailureAnalysis

logger = logging.getLogger(__name__)

FAILURE_LABELS = {
    "none": "None",
    "spaghetti": "Spaghetti extrusion",
    "detached_print": "Detached print",
    "detached_support": "Detached support",
    "layer_shift": "Layer shift",
    "midair_printing": "Printing in mid-air",
    "warping": "Severe warping",
    "nozzle_blob": "Nozzle blob",
    "unknown": "Unknown failure",
}


class DiscordNotifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def format_message(analysis: FailureAnalysis, state: PrinterState) -> str:
        label = FAILURE_LABELS.get(analysis.failure_type, analysis.failure_type)
        lines = ["**POSSIBLE PRINT FAILURE**", ""]
        lines.append(f"Printer: P1S")
        if state.file_name:
            lines.append(f"Print: {state.file_name}")
        if state.progress is not None:
            lines.append(f"Progress: {state.progress}%")
        if state.layer is not None:
            total = f" / {state.total_layers}" if state.total_layers else ""
            lines.append(f"Layer: {state.layer}{total}")

        lines += [
            "",
            f"Detected: {label}",
            f"Severity: {analysis.severity}",
            f"Confidence: {analysis.confidence * 100:.0f}%",
            "",
            "Observation:",
            analysis.explanation,
            "",
            "Check Bambu Handy before continuing. This service does not pause "
            "the printer.",
        ]
        return "\n".join(lines)

    async def send_failure(
        self,
        analysis: FailureAnalysis,
        frame: ImageFrame | None,
        state: PrinterState,
    ) -> bool:
        """Returns True when Discord accepted the alert. Never raises: a
        failed notification is logged, not fatal."""
        content = self.format_message(analysis, state)
        client = self._client or httpx.AsyncClient(timeout=20.0)

        try:
            if frame is not None:
                response = await client.post(
                    self.settings.discord_webhook_url,
                    data={"payload_json": json.dumps({"content": content})},
                    files={"file": ("failure.jpg", frame.jpeg, "image/jpeg")},
                )
            else:
                response = await client.post(
                    self.settings.discord_webhook_url, json={"content": content}
                )
        except Exception as exc:
            logger.error("Discord notification failed: %s", exc)
            return False
        finally:
            if self._owns_client:
                try:
                    await client.aclose()
                except Exception:
                    pass

        if response.status_code >= 300:
            logger.error(
                "Discord rejected the alert: %s %s", response.status_code, response.text
            )
            return False
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/notifications -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/notifications tests/notifications
git commit -m "Add Discord webhook notifier

Attaches the triggering frame so the user can judge the model's
reasoning without walking to the printer, and states explicitly
that the service does not pause the printer. A failed or rejected
POST is logged and returns False rather than raising, so a Discord
outage cannot end a monitoring session."
```

---

### Task 8: Monitor loop and entrypoint

**Files:**
- Create: `app/detection/monitor.py`, `app/main.py`
- Create: `tests/detection/test_monitor.py`

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: `app.detection.monitor.PrintMonitor` with
  `__init__(self, settings, printer, camera, analyzer, notifier, detector=None)`,
  `async run_once(self) -> str` returning one of
  `"idle"`, `"warmup"`, `"capture_failed"`, `"analyzed"`, `"alerted"`, `"paused"`, and
  `async run_forever(self) -> None`. Also `app.main.main()`.

`run_once` is the unit of test: one pass of the loop with no sleeping, so the
full pipeline can be exercised against stubs.

- [ ] **Step 1: Write the failing test**

```python
# tests/detection/test_monitor.py
from datetime import datetime, timezone

import pytest

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.detection.monitor import PrintMonitor
from app.vision.analyzer import AnalysisResult
from app.vision.schemas import FailureAnalysis


def make_settings(tmp_path, **over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u", data_dir=tmp_path)
    base.update(over)
    return Settings(**base)


class StubPrinter:
    def __init__(self, state: PrinterState):
        self.state = state
        self.connected = True

    def connect(self): ...
    def disconnect(self): ...


class StubCamera:
    def __init__(self, fail=False):
        self.fail = fail
        self.captures = 0

    async def capture(self) -> ImageFrame:
        self.captures += 1
        if self.fail:
            raise OSError("camera unreachable")
        return ImageFrame(timestamp=datetime.now(timezone.utc), jpeg=b"\xff\xd8x\xff\xd9")


class StubAnalyzer:
    def __init__(self, *results):
        self.queue = list(results)
        self.calls = 0

    async def analyze(self, frames, state, interval) -> AnalysisResult:
        self.calls += 1
        return self.queue.pop(0) if self.queue else self.queue_default()

    @staticmethod
    def queue_default() -> AnalysisResult:
        return AnalysisResult(analysis=None, model="m")


class StubNotifier:
    def __init__(self):
        self.sent = []

    async def send_failure(self, analysis, frame, state) -> bool:
        self.sent.append(analysis)
        return True


def running_state() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 10, "layer_num": 5,
                    "total_layer_num": 100, "subtask_name": "part"})
    return s


def result(status="failure", confidence=0.93, failure_type="spaghetti") -> AnalysisResult:
    return AnalysisResult(
        analysis=FailureAnalysis(status=status, confidence=confidence,
                                 failure_type=failure_type,
                                 severity="high", explanation="e"),
        input_tokens=100, output_tokens=20, model="claude-sonnet-5",
        stop_reason="end_turn",
    )


def build(tmp_path, printer, camera, analyzer, notifier, **over) -> PrintMonitor:
    return PrintMonitor(make_settings(tmp_path, **over), printer, camera,
                        analyzer, notifier)


async def test_idle_printer_does_not_capture(tmp_path):
    cam = StubCamera()
    m = build(tmp_path, StubPrinter(PrinterState()), cam, StubAnalyzer(), StubNotifier())
    assert await m.run_once() == "idle"
    assert cam.captures == 0, "must not touch the camera when not printing"


async def test_warmup_captures_but_does_not_analyze(tmp_path):
    an = StubAnalyzer()
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(), an, StubNotifier())
    assert await m.run_once() == "warmup"
    assert await m.run_once() == "warmup"
    assert an.calls == 0, "no analysis until the buffer holds 3 frames"
    assert await m.run_once() == "analyzed"
    assert an.calls == 1


async def test_capture_failure_is_skipped_not_fatal(tmp_path):
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(fail=True),
              StubAnalyzer(), StubNotifier())
    assert await m.run_once() == "capture_failed"
    assert m.next_interval == 45, "a camera failure must not change cadence"


async def test_two_matching_failures_alert_once(tmp_path):
    an = StubAnalyzer(result(), result(), result(), result())
    note = StubNotifier()
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(), an, note)
    for _ in range(2):
        await m.run_once()
    assert await m.run_once() == "analyzed"  # buffer fills, first detection
    assert m.next_interval == 10
    assert await m.run_once() == "alerted"
    assert len(note.sent) == 1
    assert note.sent[0].failure_type == "spaghetti"


async def test_paused_printer_suspends_analysis(tmp_path):
    state = running_state()
    an = StubAnalyzer()
    cam = StubCamera()
    m = build(tmp_path, StubPrinter(state), cam, an, StubNotifier())
    state.apply_report({"gcode_state": "PAUSE"})
    assert await m.run_once() == "paused"
    assert cam.captures == 0


async def test_session_created_and_closed_across_print(tmp_path):
    state = running_state()
    printer = StubPrinter(state)
    m = build(tmp_path, printer, StubCamera(), StubAnalyzer(), StubNotifier())
    await m.run_once()
    assert m.session is not None
    directory = m.session.directory
    state.apply_report({"gcode_state": "FINISH"})
    await m.run_once()
    assert m.session is None, "session must close when the print ends"
    import json
    meta = json.loads((directory / "metadata.json").read_text())
    assert meta["final_state"] == "FINISH"


async def test_new_print_starts_new_session(tmp_path):
    state = running_state()
    m = build(tmp_path, StubPrinter(state), StubCamera(), StubAnalyzer(), StubNotifier())
    await m.run_once()
    first = m.session.directory
    state.apply_report({"gcode_state": "FINISH"})
    await m.run_once()
    state.apply_report({"gcode_state": "RUNNING", "subtask_name": "second"})
    await m.run_once()
    assert m.session is not None
    assert m.session.directory != first


async def test_buffer_cleared_between_sessions(tmp_path):
    state = running_state()
    an = StubAnalyzer()
    m = build(tmp_path, StubPrinter(state), StubCamera(), an, StubNotifier())
    for _ in range(2):
        await m.run_once()
    state.apply_report({"gcode_state": "FINISH"})
    await m.run_once()
    state.apply_report({"gcode_state": "RUNNING", "subtask_name": "second"})
    assert await m.run_once() == "warmup", "stale frames must not seed a new print"
    assert an.calls == 0


async def test_notification_failure_does_not_raise(tmp_path):
    class Boom:
        async def send_failure(self, analysis, frame, state):
            raise RuntimeError("discord down")

    an = StubAnalyzer(result(), result(), result(), result())
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(), an, Boom())
    for _ in range(3):
        await m.run_once()
    assert await m.run_once() == "alerted", "alert path must survive a notifier error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/detection/test_monitor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.detection.monitor'`

- [ ] **Step 3: Write app/detection/monitor.py**

```python
"""The monitoring loop.

Owns session lifecycle and sequencing. All policy about whether something is
a failure lives in FailureDetector; all policy about what a failure looks
like lives in the vision prompt. This module only decides when to look.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.bambu.models import PAUSE, PrinterState
from app.config import Settings
from app.detection.confirmation import FailureDetector
from app.detection.history import SnapshotBuffer
from app.storage.session import PrintSession

logger = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 5


class PrintMonitor:
    def __init__(
        self,
        settings: Settings,
        printer,
        camera,
        analyzer,
        notifier,
        detector: FailureDetector | None = None,
    ):
        self.settings = settings
        self.printer = printer
        self.camera = camera
        self.analyzer = analyzer
        self.notifier = notifier
        self.detector = detector or FailureDetector(settings)
        self.buffer = SnapshotBuffer(settings.frame_history)
        self.next_interval = settings.normal_interval
        self.session: PrintSession | None = None

    @property
    def state(self) -> PrinterState:
        return self.printer.state

    async def run_forever(self) -> None:
        self.printer.connect()
        try:
            while True:
                outcome = await self.run_once()
                delay = (
                    IDLE_POLL_SECONDS
                    if outcome in ("idle", "paused")
                    else self.next_interval
                )
                await asyncio.sleep(delay)
        finally:
            self._end_session(self.state.state)
            self.printer.disconnect()

    async def run_once(self) -> str:
        state = self.state

        if state.state == PAUSE:
            # Suspend analysis but keep the session open; the print may resume.
            return "paused"

        if not state.is_printing:
            self._end_session(state.state)
            return "idle"

        if self.session is None:
            self._start_session(state)

        try:
            frame = await self.camera.capture()
        except Exception as exc:
            # A capture failure is a skipped check, at the current cadence.
            logger.warning("camera capture failed: %s", exc)
            return "capture_failed"

        self.buffer.add(frame)
        if not self.buffer.ready():
            return "warmup"

        result = await self.analyzer.analyze(
            self.buffer.latest(self.settings.frame_history), state, self.next_interval
        )
        assert self.session is not None
        decision = self.detector.process(result.analysis, self.session.id)
        self.next_interval = decision.next_interval

        frame_path = self.session.save_frame(frame)
        self.session.record(result, decision, frame_path)

        if not decision.alert or decision.analysis is None:
            return "analyzed"

        logger.warning("ALERT: %s", decision.reason)
        try:
            await self.notifier.send_failure(decision.analysis, frame, state)
        except Exception as exc:
            # The detection is already recorded; a delivery failure must not
            # end the session.
            logger.error("could not deliver alert: %s", exc)
        return "alerted"

    def _start_session(self, state: PrinterState) -> None:
        self.buffer.clear()
        self.detector.reset()
        self.next_interval = self.settings.normal_interval
        self.session = PrintSession.create(
            self.settings, state.file_name, datetime.now(timezone.utc)
        )
        logger.info("monitoring session %s (%s)", self.session.id, state.file_name)

    def _end_session(self, final_state: str | None) -> None:
        if self.session is None:
            return
        logger.info(
            "session %s ended (%s): %d checks, %d alerts, %d/%d tokens",
            self.session.id,
            final_state,
            self.session.checks,
            self.session.alerts,
            self.session.total_input_tokens,
            self.session.total_output_tokens,
        )
        self.session.close(final_state)
        self.session = None
        self.buffer.clear()
        self.detector.reset()
        self.next_interval = self.settings.normal_interval
```

- [ ] **Step 4: Write app/main.py**

```python
"""Entrypoint."""

from __future__ import annotations

import asyncio
import logging
import signal

from app.bambu.camera import P1SCamera
from app.bambu.mqtt_client import BambuMqttClient
from app.config import get_settings
from app.detection.monitor import PrintMonitor
from app.notifications.discord import DiscordNotifier
from app.vision.analyzer import VisionAnalyzer

logger = logging.getLogger("bambu_watch")


def build_monitor() -> PrintMonitor:
    settings = get_settings()
    return PrintMonitor(
        settings=settings,
        printer=BambuMqttClient(settings),
        camera=P1SCamera(settings),
        analyzer=VisionAnalyzer(settings),
        notifier=DiscordNotifier(settings),
    )


async def run() -> None:
    settings = get_settings()
    monitor = build_monitor()
    logger.info(
        "bambu-watch starting: printer=%s model=%s interval=%ss width=%s",
        settings.bambu_host,
        settings.vision_model,
        settings.normal_interval,
        settings.frame_upload_width,
    )
    logger.info("advisory only: this service never pauses the printer")

    task = asyncio.create_task(monitor.run_forever())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:
            pass

    try:
        await task
    except asyncio.CancelledError:
        logger.info("shutting down")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -v`
Expected: all tests pass, 60+ total

- [ ] **Step 6: Commit**

```bash
git add app/detection/monitor.py app/main.py tests/detection/test_monitor.py
git commit -m "Add monitor loop and entrypoint

run_once is one pass of the pipeline with no sleeping, so the whole
flow is testable against stubs. The monitor owns session lifecycle
and cadence only; whether something is a failure stays in the
detector and the prompt.

A camera failure is a skipped check at the current cadence, a
notifier failure is logged after the detection is already
persisted, and the frame buffer and detector are cleared between
sessions so stale frames cannot seed a new print."
```

---

### Task 9: Hardware probe script

Throwaway diagnostic. Validates the two undocumented protocols against the
real printer and writes fixtures. Not imported by the service.

**Files:**
- Create: `scripts/probe_printer.py`

**Interfaces:**
- Consumes: `Settings`, `BambuMqttClient`, `P1SCamera`.
- Produces: nothing the service imports. Writes `fixtures/live/report.json` and
  `fixtures/live/frame.jpg`.

- [ ] **Step 1: Write scripts/probe_printer.py**

```python
"""Validate the undocumented P1S protocols against real hardware.

Run this once, with a populated .env, before trusting the service:

    .venv/bin/python scripts/probe_printer.py

Writes fixtures/live/report.json and fixtures/live/frame.jpg so the MQTT
report shape and the camera handshake can be verified. Prints the resolution
of the captured frame, which is what determines per-check token cost.

This script reads the access code from .env and never prints it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bambu.camera import P1SCamera  # noqa: E402
from app.bambu.mqtt_client import BambuMqttClient  # noqa: E402
from app.config import get_settings  # noqa: E402

OUT = Path("fixtures/live")


async def probe_mqtt(settings) -> bool:
    print("--- MQTT ---")
    client = BambuMqttClient(settings)
    captured: list[dict] = []
    original = client.handle_payload

    def spy(payload: bytes) -> None:
        try:
            message = json.loads(payload)
            if isinstance(message, dict) and isinstance(message.get("print"), dict):
                captured.append(message)
        except ValueError:
            pass
        original(payload)

    client.handle_payload = spy  # type: ignore[method-assign]
    client.connect()

    for _ in range(30):
        await asyncio.sleep(1)
        if captured:
            break

    client.disconnect()

    if not captured:
        print("FAIL: no print reports in 30s.")
        print("  Check host, serial, and access code, and that port 8883 is open.")
        return False

    richest = max(captured, key=lambda m: len(m["print"]))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(richest, indent=2), encoding="utf-8")

    print(f"OK: {len(captured)} reports; richest has "
          f"{len(richest['print'])} keys -> {OUT / 'report.json'}")
    print(f"  state={client.state.state} progress={client.state.progress} "
          f"layer={client.state.layer}/{client.state.total_layers} "
          f"file={client.state.file_name}")

    expected = {"gcode_state", "mc_percent", "layer_num", "total_layer_num",
                "subtask_name", "mc_remaining_time"}
    missing = expected - set(richest["print"])
    if missing:
        print(f"  NOTE: keys absent from this report: {sorted(missing)}")
    return True


async def probe_camera(settings) -> bool:
    print("--- Camera ---")
    try:
        frame = await P1SCamera(settings).capture()
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        print("  The port-6000 handshake is undocumented and may need correcting")
        print("  in app/bambu/camera.py (build_auth_packet / read_frame).")
        return False

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "frame.jpg"
    path.write_bytes(frame.jpeg)
    print(f"OK: {len(frame.jpeg)} bytes -> {path}")

    try:
        from PIL import Image

        with Image.open(path) as img:
            w, h = img.size
        print(f"  resolution {w}x{h}, about {w * h // 750} tokens per frame")
        print(f"  downscaled to width {settings.frame_upload_width}: about "
              f"{settings.frame_upload_width * round(h * settings.frame_upload_width / w) // 750}"
              " tokens per frame")
    except Exception as exc:
        print(f"  WARNING: captured bytes are not a readable image: {exc}")
        return False
    return True


async def main() -> int:
    try:
        settings = get_settings()
    except Exception as exc:
        print(f"Could not load settings: {exc}")
        print("Copy .env.example to .env and fill it in.")
        return 2

    print(f"Probing {settings.bambu_host} (serial {settings.bambu_serial})\n")
    mqtt_ok = await probe_mqtt(settings)
    print()
    camera_ok = await probe_camera(settings)

    print("\n--- Result ---")
    print(f"MQTT:   {'OK' if mqtt_ok else 'FAIL'}")
    print(f"Camera: {'OK' if camera_ok else 'FAIL'}")
    if mqtt_ok and camera_ok:
        print("\nBoth protocols verified. The service is safe to start.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: Verify it imports and fails cleanly without credentials**

Run: `env -i PATH="$PATH" .venv/bin/python scripts/probe_printer.py; echo "exit=$?"`
Expected: prints "Could not load settings" guidance and `exit=2`. It must not
traceback.

- [ ] **Step 3: Commit**

```bash
git add scripts/probe_printer.py
git commit -m "Add hardware probe script

Validates the two undocumented protocols against the real printer
and writes fixtures. Reports the captured frame resolution and the
resulting per-frame token count, which is what determines API cost
per check. Reads the access code from .env and never prints it."
```

---

### Task 10: Docker deployment and README

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `README.md`

**Interfaces:**
- Consumes: the full application.
- Produces: a container that restarts after a Pi reboot and runs as a non-root user.

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
FROM python:3.12-slim

# Pillow needs libjpeg at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libjpeg62-turbo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Non-root, per the design. The data volume must be writable by this user.
RUN useradd --create-home --uid 10001 watcher \
    && mkdir -p /app/data/sessions \
    && chown -R watcher:watcher /app/data
USER watcher

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.main"]
```

- [ ] **Step 2: Write docker-compose.yml**

```yaml
services:
  bambu-watch:
    build: .
    container_name: bambu-watch
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/app/data
    # No ports published. The service makes only outbound connections:
    # the printer on the LAN, the Claude API, and Discord.
```

- [ ] **Step 3: Write .dockerignore**

```
.venv/
.git/
.env
data/
fixtures/
docs/
tests/
scripts/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.DS_Store
```

- [ ] **Step 4: Write README.md**

Include, in this order: what it does and the explicit statement that it never
pauses the printer; the alert flow diagram from the brief; hardware and API
prerequisites; setup (`cp .env.example .env`, where to find the LAN access
code on the printer, `uv venv`, `uv pip install -e ".[dev]"`); running the
probe script first and what its output means; `docker compose up -d`; a
configuration table listing every `Settings` field with its default and one
line on what it does; a cost section giving the measured formula
`(width * height) / 750` tokens per frame, three frames per check, and the
estimated per-8-hour-print figures with the levers that reduce them
(`NORMAL_INTERVAL`, `FRAME_UPLOAD_WIDTH`, `VISION_EFFORT`, `VISION_MODEL`);
the session data layout and how to compute the false-positive rate from
`detections.jsonl`; and a troubleshooting section covering MQTT connect
refusal, an empty `PrinterState`, and camera handshake failure pointing at
`build_auth_packet`.

- [ ] **Step 5: Verify the image builds and the suite still passes**

Run: `docker build -t bambu-watch:test . && .venv/bin/python -m pytest -q`
Expected: image builds; all tests pass.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore README.md
git commit -m "Add Docker deployment and README

Runs as a non-root user with restart unless-stopped so the service
returns after a Pi reboot. No ports are published; the container
makes only outbound connections to the printer, the Claude API, and
Discord. The README documents the per-frame token formula and the
four levers that reduce API cost per print."
```

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|---|---|
| 3.1 Claude provider, parse, adaptive thinking, effort, Optional/refusal guards | 4 |
| 3.2 Downscaling, effort low, usage logging, no two-tier | 4, 5 |
| 3.3 Private repo, gitignored `.env`, `.env.example` placeholders | 1, done in scaffold commit |
| 4.1 Suspicious state leak | 3 (`test_regression_low_confidence_does_not_pin_suspicious_interval`) |
| 4.2 Disagreeing types deadlock | 3 (`test_regression_disagreeing_failure_type_replaces_candidate`) |
| 4.3 No suspicion timeout | 3 (`test_regression_suspicion_times_out`) |
| 4.4 Missing MQTT pushall | 6 (`test_pushall_publishes_to_request_topic`) |
| 5.1 `subtask_name` preferred | 2 (`test_subtask_name_preferred_over_gcode_file`) |
| 5.2 Network resilience both edges | 6 (paho reconnect), 8 (`test_capture_failure_is_skipped_not_fatal`) |
| 5.3 Camera protocol risk isolated + probe | 6 (`Camera` protocol), 9 |
| 6 Detection policy defaults | 1 (`test_defaults_match_detection_policy`) |
| 7 Testing: detector heaviest, fixtures, no credit spent | 3, 2, 4 (stub client) |
| 8 Out of scope | no tasks, correct |
| Brief s11 Print sessions | 5 |
| Brief s16 Docker, non-root, restart | 10 |

No gaps.

**2. Placeholder scan**

No TBD/TODO. Every code step carries real content. Task 10 Step 4 describes
README sections rather than showing prose; that is a documentation step whose
content is fully enumerated, not a code placeholder.

**3. Type consistency**

- `Decision` is produced in Task 3 and consumed in Tasks 5 and 8 with fields
  `alert`, `next_interval`, `reason`, `analysis`. Consistent.
- `AnalysisResult` is produced in Task 4 and consumed in Tasks 5 and 8 with
  fields `analysis`, `input_tokens`, `output_tokens`, `model`, `stop_reason`.
  Consistent.
- `FailureDetector.process(result, session_id)` takes `FailureAnalysis | None`.
  Task 8 passes `result.analysis`, which is that type. Consistent.
- `PrintSession.record(result, decision, frame_path)` matches the Task 8 call.
- `SnapshotBuffer.latest(n)` is called with `settings.frame_history` in Task 8
  and the buffer is constructed with the same value. Consistent.
- `Camera.capture()` is async in Task 6 and awaited in Task 8. Consistent.
- One note: `FailureDetector` exposes `self.settings`, which Task 3's tests
  read as `detector.settings.suspicion_max_checks`. Present in the
  implementation.
