"""The monitoring loop.

Owns session lifecycle and sequencing. All policy about whether something is
a failure lives in FailureDetector; all policy about what a failure looks
like lives in the vision prompt. This module only decides when to look.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from app.bambu.models import PAUSE, PREPARE, PrinterState
from app.bambu.slice_info import SliceInfo
from app.config import Settings
from app.detection.confirmation import FailureDetector
from app.detection.history import SnapshotBuffer
from app.storage.prints import FilamentRow
from app.storage.session import PrintSession

logger = logging.getLogger(__name__)

IDLE_POLL_SECONDS = 5
# How long one pass will wait on an in-flight slice fetch before leaving it
# pending. Keeps a fast fetch to a single pass without blocking the loop.
COLLECT_GRACE_SECONDS = 0.05


class PrintMonitor:
    def __init__(
        self,
        settings: Settings,
        printer,
        camera,
        analyzer,
        notifier,
        detector: FailureDetector | None = None,
        repository=None,
        ftp=None,
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
        self.repository = repository
        self.ftp = ftp
        self.slice_info: SliceInfo | None = None
        self._slice_source = "unavailable"
        self._slice_task: asyncio.Task | None = None
        self._slice_attempted_for: str | None = None

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
                    if outcome in ("idle", "paused", "preparing")
                    else self.next_interval
                )
                await asyncio.sleep(delay)
        finally:
            await self._end_session(self.state.state)
            self.printer.disconnect()

    async def run_once(self) -> str:
        state = self.state

        # First, so a transfer still in flight when the printer leaves PREPARE
        # is cancelled rather than running into a live print.
        await self._tick_slice_fetch(state)

        if state.state == PREPARE:
            # Nothing is extruding yet. This is the only window in which the
            # sliced file is fetched while a print is pending.
            return "preparing"

        if state.state == PAUSE:
            # Suspend analysis but keep the session open; the print may resume.
            return "paused"

        if not state.is_printing:
            await self._end_session(state.state)
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

        if self.repository is not None:
            try:
                self.repository.insert_running(
                    self.session.id,
                    state.file_name,
                    self.session.started_at,
                    str(self.session.directory),
                )
            except Exception as exc:
                logger.error("could not open print history row: %s", exc)

    async def _end_session(self, final_state: str | None) -> None:
        if self.session is None:
            self._reset_slice_state()
            return

        session, self.session = self.session, None
        state = self.state

        # The print is over, so a fetch here cannot contend with printing.
        if (
            self.slice_info is None
            and self.ftp is not None
            and self.settings.enable_slice_fetch
            and session.file_name
        ):
            self.slice_info = await self.ftp.fetch_slice_info(session.file_name)
            self._slice_source = "ftps" if self.slice_info else "unavailable"

        logger.info(
            "session %s ended (%s): %d checks, %d alerts, %d in / %d out tokens",
            session.id,
            final_state,
            session.checks,
            session.alerts,
            session.total_input_tokens,
            session.total_output_tokens,
        )
        session.close(final_state)

        if self.repository is not None:
            self._write_print_row(session, final_state, state)

        self.buffer.clear()
        self.detector.reset()
        self.next_interval = self.settings.normal_interval
        self._reset_slice_state()

    async def _tick_slice_fetch(self, state: PrinterState) -> None:
        """Advance the PREPARE-window fetch: launch, collect, or cancel.

        Called on every pass of the loop. A fetch still running when the
        printer leaves PREPARE is cancelled, which is what keeps FTPS away
        from a live print.
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

        # Yield so a task created on this pass can actually start, and give a
        # quick fetch the chance to finish within one tick. A slow transfer
        # stays pending and is collected on a later pass -- or cancelled above
        # if the printer starts printing first. Without this the state machine
        # would depend on the caller sleeping between passes.
        await asyncio.wait({self._slice_task}, timeout=COLLECT_GRACE_SECONDS)

        if self._slice_task.done():
            task, self._slice_task = self._slice_task, None
            self._slice_attempted_for = state.file_name
            try:
                self.slice_info = task.result()
            except Exception:
                self.slice_info = None
            self._slice_source = "ftps" if self.slice_info else "unavailable"
            logger.info(
                "slice info %s for %r",
                "obtained" if self.slice_info else "unavailable",
                state.file_name,
            )

    def _write_print_row(self, session, final_state, state) -> None:
        info = self.slice_info
        try:
            outcome = self.repository.finalize(
                session.id,
                final_state=final_state,
                ended_at=datetime.now(UTC),
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
