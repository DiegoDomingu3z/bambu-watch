"""The monitoring loop.

Owns session lifecycle and sequencing. All policy about whether something is
a failure lives in FailureDetector; all policy about what a failure looks
like lives in the vision prompt. This module only decides when to look.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

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
            self.settings, state.file_name, datetime.now(UTC)
        )
        logger.info("monitoring session %s (%s)", self.session.id, state.file_name)

    def _end_session(self, final_state: str | None) -> None:
        if self.session is None:
            return
        logger.info(
            "session %s ended (%s): %d checks, %d alerts, %d in / %d out tokens",
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
