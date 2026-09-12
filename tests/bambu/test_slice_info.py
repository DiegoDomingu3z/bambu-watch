import io
import zipfile

import pytest

from app.bambu.slice_info import (
    SLICE_INFO_MEMBER,
    extract_slice_info,
    normalize_color,
    parse_slice_info_xml,
)

FOUR_COLOUR = b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="1" type="PLA"  color="#FF0000" used_m="40.0" used_g="120.0"/>
    <filament id="2" type="PLA"  color="#00FF00" used_m="31.8" used_g="95.5"/>
    <filament id="3" type="PETG" color="#0000FF" used_m="26.0" used_g="80.0"/>
    <filament id="4" type="PLA"  color="#FFFFFF" used_m="15.4" used_g="44.5"/>
  </plate>
</config>
"""

SINGLE = b"""<?xml version="1.0"?>
<config><plate>
  <filament id="1" type="PLA" color="000000FF" used_m="113.2" used_g="340.0"/>
</plate></config>
"""


def make_3mf(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_parses_four_filaments():
    info = parse_slice_info_xml(FOUR_COLOUR)
    assert len(info.filaments) == 4
    assert [f.slot for f in info.filaments] == [1, 2, 3, 4]
    assert info.filaments[2].filament_type == "PETG"
    assert info.total_grams == pytest.approx(340.0)
    assert info.total_meters == pytest.approx(113.2)


def test_colors_are_normalized():
    assert normalize_color("#FF0000") == "#FF0000"
    assert normalize_color("000000FF") == "#000000", "RGBA hex drops alpha"
    assert normalize_color("ff0000") == "#FF0000"
    assert normalize_color(None) is None
    assert normalize_color("") is None
    assert normalize_color("not a colour") is None
    assert normalize_color("#12345") is None


def test_single_filament_rgba_color():
    info = parse_slice_info_xml(SINGLE)
    assert len(info.filaments) == 1
    assert info.filaments[0].color == "#000000"
    assert info.total_grams == pytest.approx(340.0)


def test_malformed_xml_returns_none():
    assert parse_slice_info_xml(b"<config><unclosed>") is None
    assert parse_slice_info_xml(b"") is None
    assert parse_slice_info_xml(b"not xml at all") is None


def test_no_filament_elements_returns_none():
    assert parse_slice_info_xml(b"<config><plate/></config>") is None


def test_missing_numbers_do_not_crash():
    info = parse_slice_info_xml(
        b'<config><plate><filament id="1" type="PLA"/></plate></config>')
    assert info.filaments[0].used_grams is None
    assert info.total_grams is None, "no data must not total to zero"


def test_unparseable_numbers_become_none():
    info = parse_slice_info_xml(
        b'<config><plate><filament id="1" used_g="abc" used_m="-"/></plate></config>')
    assert info.filaments[0].used_grams is None
    assert info.filaments[0].used_meters is None


def test_partial_numbers_total_what_is_known():
    info = parse_slice_info_xml(
        b'<config><plate><filament id="1" used_g="100"/>'
        b'<filament id="2"/></plate></config>')
    assert info.total_grams == pytest.approx(100.0)


def test_filaments_without_ids_are_numbered_by_order():
    info = parse_slice_info_xml(
        b'<config><plate><filament used_g="1"/><filament used_g="2"/></plate></config>')
    assert [f.slot for f in info.filaments] == [1, 2], "primary key must stay unique"


def test_duplicate_ids_are_rejected_entirely():
    info = parse_slice_info_xml(
        b'<config><plate><filament id="1" used_g="1"/>'
        b'<filament id="1" used_g="2"/></plate></config>')
    assert info is None, "a colliding key means the file is malformed"


def test_extract_from_3mf():
    archive = make_3mf({
        "3D/3dmodel.model": b"<model/>",
        SLICE_INFO_MEMBER: FOUR_COLOUR,
    })
    info = extract_slice_info(archive)
    assert len(info.filaments) == 4
    assert info.total_grams == pytest.approx(340.0)


def test_extract_missing_member_returns_none():
    assert extract_slice_info(make_3mf({"3D/3dmodel.model": b"<model/>"})) is None


def test_extract_from_non_zip_returns_none():
    assert extract_slice_info(b"definitely not a zip") is None


def test_extract_from_empty_bytes_returns_none():
    assert extract_slice_info(b"") is None


def test_extract_from_truncated_zip_returns_none():
    archive = make_3mf({SLICE_INFO_MEMBER: FOUR_COLOUR})
    assert extract_slice_info(archive[: len(archive) // 2]) is None
