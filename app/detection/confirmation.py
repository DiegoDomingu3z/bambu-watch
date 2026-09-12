"""Failure confirmation state machine.

Pure logic: no I/O, no network, no clock reads except through the injected
`now` callable. A single model response never produces an alert; a second
matching high-confidence detection is required.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.vision.schemas import FailureAnalysis

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
