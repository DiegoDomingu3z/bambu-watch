"""Normalized printer state. Raw Bambu MQTT payloads never leave this module."""

from __future__ import annotations

from dataclasses import dataclass
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
