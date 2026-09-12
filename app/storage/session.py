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
from datetime import UTC, datetime
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
        self, result: AnalysisResult, decision: Decision, frame_path: Path | None
    ) -> None:
        analysis = result.analysis
        self.checks += 1
        self.total_input_tokens += result.input_tokens
        self.total_output_tokens += result.output_tokens
        if decision.alert:
            self.alerts += 1

        row = {
            "timestamp": datetime.now(UTC).isoformat(),
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
            "frame": str(frame_path.relative_to(self.directory)) if frame_path else None,
        }
        try:
            with (self.directory / "detections.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError as exc:
            logger.warning("could not append detection: %s", exc)

    def close(self, final_state: str | None) -> None:
        self._write_metadata(final_state=final_state, ended_at=datetime.now(UTC))

    def _write_metadata(self, final_state: str | None, ended_at: datetime | None) -> None:
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
