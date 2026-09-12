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
        lines.append(f"Current layer: {state.layer}" + (f" of {total}" if total else ""))
    if state.total_layers is not None:
        lines.append(f"Total layers: {state.total_layers}")

    lines.append("")
    for i in range(count):
        age = (count - 1 - i) * interval
        when = "current" if age == 0 else f"approximately {age} seconds ago"
        lines.append(f"Image {i + 1}: {when}")

    return "\n".join(lines)
