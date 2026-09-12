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
        return OUTCOME_COMPLETED if progress >= COMPLETION_PROGRESS else OUTCOME_STOPPED
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

    # Layer 0 counts as missing: a print that died before its first layer
    # consumed nothing worth recording and has no reliable figure.
    if not layer_at_end or not total_layers or total_layers <= 0:
        return None, METHOD_UNKNOWN

    fraction = min(1.0, max(0.0, layer_at_end / total_layers))
    return planned_grams * fraction, METHOD_LAYER_FRACTION
