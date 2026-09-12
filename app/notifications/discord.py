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
        lines = ["**POSSIBLE PRINT FAILURE**", "", "Printer: P1S"]
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
