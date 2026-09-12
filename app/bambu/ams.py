"""Normalize the MQTT AMS block into flat slots.

Slot numbering is unit * 4 + tray so a second AMS unit does not collide with
the first. The external spool is slot -1, matching print_filaments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.bambu.slice_info import normalize_color

logger = logging.getLogger(__name__)

TRAYS_PER_UNIT = 4
EXTERNAL_SLOT = -1


@dataclass
class AmsSlot:
    slot: int
    filament_type: str | None = None
    color: str | None = None
    remain: int | None = None


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _slot_from(tray: dict, slot: int) -> AmsSlot | None:
    filament_type = tray.get("tray_type") or None
    color = normalize_color(tray.get("tray_color"))
    if not filament_type and not color:
        return None

    remain = _as_int(tray.get("remain"))
    if remain is not None and remain < 0:
        remain = None  # -1 means unknown, not empty
    return AmsSlot(slot=slot, filament_type=filament_type, color=color, remain=remain)


def parse_ams(print_data: dict) -> list[AmsSlot]:
    """Never raises. An unexpected shape yields an empty list."""
    slots: list[AmsSlot] = []

    block = print_data.get("ams")
    units = block.get("ams") if isinstance(block, dict) else None
    if isinstance(units, list):
        for index, unit in enumerate(units):
            if not isinstance(unit, dict):
                continue
            unit_id = _as_int(unit.get("id"))
            base = (unit_id if unit_id is not None else index) * TRAYS_PER_UNIT
            trays = unit.get("tray")
            if not isinstance(trays, list):
                continue
            for tray_index, tray in enumerate(trays):
                if not isinstance(tray, dict):
                    continue
                tray_id = _as_int(tray.get("id"))
                number = base + (tray_id if tray_id is not None else tray_index)
                parsed = _slot_from(tray, number)
                if parsed is not None:
                    slots.append(parsed)

    external = print_data.get("vt_tray")
    if isinstance(external, dict):
        parsed = _slot_from(external, EXTERNAL_SLOT)
        if parsed is not None:
            slots.append(parsed)

    return slots
