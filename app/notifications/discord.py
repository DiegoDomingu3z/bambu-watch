"""Discord webhook notifier.

The alert has to carry enough for the user to judge the model's reasoning
without walking to the printer, and it has to say where to act. V1 uses a
webhook rather than a bot because nothing needs to be received.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field

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


@dataclass
class PrintStarted:
    """Everything known when a print begins. Material fields are None unless
    the sliced file was fetched during PREPARE."""

    file_name: str | None = None
    total_layers: int | None = None
    planned_grams: float | None = None
    planned_cost: float | None = None
    filaments: list[tuple[str | None, str | None]] = field(default_factory=list)


@dataclass
class PrintFinished:
    """Everything known when a print ends."""

    file_name: str | None = None
    outcome: str = "unknown"
    duration_seconds: int | None = None
    layer_at_end: int | None = None
    total_layers: int | None = None
    consumed_grams: float | None = None
    consumed_cost: float | None = None
    estimated: bool = False
    checks: int = 0
    alerts: int = 0
    monitoring_cost: float | None = None


@dataclass
class MaterialSummary:
    """Filament figures for an alert. `estimated` is True when consumed grams
    came from a layer fraction rather than a measurement, and the rendered
    line says so."""

    planned_grams: float | None = None
    consumed_grams: float | None = None
    consumed_cost: float | None = None
    estimated: bool = True

    @property
    def renderable(self) -> bool:
        return self.consumed_grams is not None


OUTCOME_HEADINGS = {
    "completed": "PRINT FINISHED",
    "stopped": "PRINT STOPPED",
    "failed": "PRINT FAILED",
    "unknown": "PRINT ENDED",
    "running": "PRINT ENDED",
}


def _duration(seconds: int | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


class DiscordNotifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def format_message(
        analysis: FailureAnalysis,
        state: PrinterState,
        material: MaterialSummary | None = None,
    ) -> str:
        label = FAILURE_LABELS.get(analysis.failure_type, analysis.failure_type)
        lines = ["**POSSIBLE PRINT FAILURE**", "", "Printer: P1S"]
        if state.file_name:
            lines.append(f"Print: {state.file_name}")
        if state.progress is not None:
            lines.append(f"Progress: {state.progress}%")
        if state.layer is not None:
            total = f" / {state.total_layers}" if state.total_layers else ""
            lines.append(f"Layer: {state.layer}{total}")

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

        lines += [
            "",
            f"Detected: {label}",
            f"Severity: {analysis.severity}",
            f"Confidence: {analysis.confidence * 100:.0f}%",
            "",
            "Observation:",
            analysis.explanation,
            "",
            (
                "Check Bambu Handy before continuing. "
                "This service does not pause the printer."
            ),
        ]
        return "\n".join(lines)

    @staticmethod
    def format_started(event: PrintStarted) -> str:
        lines = ["**PRINT STARTED**", ""]
        lines.append(f"Print: {event.file_name}" if event.file_name else "Print: unknown")
        if event.total_layers:
            lines.append(f"Layers: {event.total_layers}")

        if event.planned_grams is not None:
            line = f"Material: {event.planned_grams:.0f}g planned"
            if event.planned_cost is not None:
                line += f", {event.planned_cost:.2f} USD"
            lines.append(line)

        if event.filaments:
            shown = ", ".join(
                " ".join(part for part in (kind, color) if part)
                for kind, color in event.filaments
            )
            if shown:
                lines.append(f"Colours: {shown}")

        lines += ["", "AI monitoring active. This service never pauses the printer."]
        return "\n".join(lines)

    @staticmethod
    def format_finished(event: PrintFinished) -> str:
        heading = OUTCOME_HEADINGS.get(event.outcome, "PRINT ENDED")
        lines = [f"**{heading}**", ""]
        lines.append(f"Print: {event.file_name}" if event.file_name else "Print: unknown")
        lines.append(f"Outcome: {event.outcome}")

        duration = _duration(event.duration_seconds)
        if duration:
            lines.append(f"Duration: {duration}")
        if event.layer_at_end is not None:
            total = f" / {event.total_layers}" if event.total_layers else ""
            lines.append(f"Layer: {event.layer_at_end}{total}")

        if event.consumed_grams is not None:
            verb = "wasted" if event.outcome in ("stopped", "failed") else "used"
            line = f"Material {verb}: {event.consumed_grams:.0f}g"
            if event.consumed_cost is not None:
                line += f" ({event.consumed_cost:.2f} USD"
                line += ", estimated)" if event.estimated else ")"
            elif event.estimated:
                line += " (estimated)"
            lines.append(line)

        lines.append(f"Checks: {event.checks}  Alerts: {event.alerts}")
        if event.monitoring_cost is not None:
            lines.append(f"Monitoring cost: {event.monitoring_cost:.2f} USD")
        return "\n".join(lines)

    async def send_print_started(self, event: PrintStarted) -> bool:
        return await self._post(self.format_started(event), None)

    async def send_print_finished(
        self, event: PrintFinished, frame: ImageFrame | None = None
    ) -> bool:
        return await self._post(self.format_finished(event), frame, "finished.jpg")

    async def send_failure(
        self,
        analysis: FailureAnalysis,
        frame: ImageFrame | None,
        state: PrinterState,
        material: MaterialSummary | None = None,
    ) -> bool:
        """Returns True when Discord accepted the alert. Never raises: a
        failed notification is logged, not fatal."""
        return await self._post(
            self.format_message(analysis, state, material), frame, "failure.jpg"
        )

    async def _post(
        self, content: str, frame: ImageFrame | None, filename: str = "frame.jpg"
    ) -> bool:
        """One POST path for every notification type. Never raises."""
        client = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            if frame is not None:
                response = await client.post(
                    self.settings.discord_webhook_url,
                    data={"payload_json": json.dumps({"content": content})},
                    files={"file": (filename, frame.jpeg, "image/jpeg")},
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
                with contextlib.suppress(Exception):
                    await client.aclose()

        if response.status_code >= 300:
            logger.error(
                "Discord rejected the message: %s %s",
                response.status_code, response.text,
            )
            return False
        return True
