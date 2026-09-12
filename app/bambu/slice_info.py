"""Parse filament usage out of a sliced 3MF.

Deliberately separated from the FTPS transport so all parsing is testable
against a synthetic archive built in-process. Everything returns None on bad
input rather than raising: a malformed file means no material figures, never a
broken monitoring session.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

SLICE_INFO_MEMBER = "Metadata/slice_info.config"
_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$")


def normalize_color(raw: str | None) -> str | None:
    """Slicers report colours as #RRGGBB or bare RRGGBBAA. Normalize to
    #RRGGBB and drop any alpha channel."""
    if not raw:
        return None
    match = _HEX.match(raw.strip())
    if not match:
        return None
    return f"#{match.group(1).upper()}"


def _as_float(raw: str | None) -> float | None:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass
class FilamentUsage:
    slot: int
    filament_type: str | None = None
    color: str | None = None
    used_grams: float | None = None
    used_meters: float | None = None


@dataclass
class SliceInfo:
    filaments: list[FilamentUsage] = field(default_factory=list)

    @property
    def total_grams(self) -> float | None:
        values = [f.used_grams for f in self.filaments if f.used_grams is not None]
        return sum(values) if values else None

    @property
    def total_meters(self) -> float | None:
        values = [f.used_meters for f in self.filaments if f.used_meters is not None]
        return sum(values) if values else None


def parse_slice_info_xml(data: bytes) -> SliceInfo | None:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        logger.warning("slice info is not parseable XML")
        return None

    filaments: list[FilamentUsage] = []
    seen: set[int] = set()

    for index, element in enumerate(root.iter("filament"), start=1):
        raw_id = _as_float(element.get("id"))
        # Filaments without an id are numbered by order of appearance so the
        # (print_id, slot) primary key stays unique.
        resolved = int(raw_id) if raw_id is not None else index
        if resolved in seen:
            logger.warning("slice info has duplicate filament id %s", resolved)
            return None
        seen.add(resolved)

        filaments.append(
            FilamentUsage(
                slot=resolved,
                filament_type=element.get("type") or None,
                color=normalize_color(element.get("color")),
                used_grams=_as_float(element.get("used_g")),
                used_meters=_as_float(element.get("used_m")),
            )
        )

    if not filaments:
        logger.warning("slice info contains no filament entries")
        return None
    return SliceInfo(filaments=filaments)


def extract_slice_info(archive: bytes) -> SliceInfo | None:
    """Pull SLICE_INFO_MEMBER out of a 3MF, which is a ZIP."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            data = zf.read(SLICE_INFO_MEMBER)
    except (zipfile.BadZipFile, KeyError, OSError, EOFError) as exc:
        logger.warning("could not read %s: %s", SLICE_INFO_MEMBER, exc)
        return None
    return parse_slice_info_xml(data)
