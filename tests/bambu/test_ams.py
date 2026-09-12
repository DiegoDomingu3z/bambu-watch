from app.bambu.ams import parse_ams
from app.bambu.models import PrinterState

REPORT = {
    "ams": {
        "ams": [{
            "id": "0",
            "tray": [
                {"id": "0", "tray_type": "PLA",  "tray_color": "FF0000FF", "remain": 84},
                {"id": "1", "tray_type": "PLA",  "tray_color": "00FF00FF", "remain": 60},
                {"id": "2", "tray_type": "PETG", "tray_color": "0000FFFF", "remain": 12},
                {"id": "3", "tray_type": "PLA",  "tray_color": "FFFFFFFF", "remain": -1},
            ],
        }],
    },
    "vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "123456FF", "remain": 50},
}


def test_parses_four_ams_trays_and_external():
    by_slot = {s.slot: s for s in parse_ams(REPORT)}
    assert {0, 1, 2, 3, -1} <= set(by_slot)
    assert by_slot[0].color == "#FF0000"
    assert by_slot[2].filament_type == "PETG"
    assert by_slot[-1].color == "#123456", "external spool is slot -1"


def test_unknown_remain_becomes_none():
    slots = {s.slot: s for s in parse_ams(REPORT)}
    assert slots[3].remain is None, "-1 means unknown, not zero"
    assert slots[0].remain == 84


def test_second_ams_unit_offsets_slot_numbers():
    report = {"ams": {"ams": [
        {"id": "0", "tray": [{"id": "0", "tray_type": "PLA"}]},
        {"id": "1", "tray": [{"id": "0", "tray_type": "ABS"}]},
    ]}}
    by_slot = {s.slot: s for s in parse_ams(report)}
    assert by_slot[0].filament_type == "PLA"
    assert by_slot[4].filament_type == "ABS", "unit 1 tray 0 is slot 4"


def test_empty_and_malformed_reports_yield_nothing():
    assert parse_ams({}) == []
    assert parse_ams({"ams": None}) == []
    assert parse_ams({"ams": {}}) == []
    assert parse_ams({"ams": {"ams": "not a list"}}) == []
    assert parse_ams({"ams": {"ams": [{"tray": "nope"}]}}) == []
    assert parse_ams({"ams": {"ams": ["not a dict"]}}) == []
    assert parse_ams({"vt_tray": "nope"}) == []


def test_empty_tray_slot_is_skipped():
    report = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}]}}
    assert parse_ams(report) == [], "a slot with no filament is not a colour"


def test_tray_with_only_a_colour_is_kept():
    report = {"ams": {"ams": [{"id": "0", "tray": [
        {"id": "0", "tray_color": "AABBCCFF"}]}]}}
    slots = parse_ams(report)
    assert len(slots) == 1
    assert slots[0].color == "#AABBCC"
    assert slots[0].filament_type is None


def test_missing_ids_fall_back_to_position():
    report = {"ams": {"ams": [{"tray": [
        {"tray_type": "PLA"}, {"tray_type": "ABS"}]}]}}
    assert [s.slot for s in parse_ams(report)] == [0, 1]


def test_printer_state_retains_ams_slots():
    state = PrinterState()
    state.apply_report(REPORT)
    assert len(state.ams_slots) == 5


def test_printer_state_ams_survives_incremental_reports():
    state = PrinterState()
    state.apply_report(REPORT)
    state.apply_report({"layer_num": 5})
    assert len(state.ams_slots) == 5, "absent ams block must not clear slots"


def test_printer_state_starts_with_no_slots():
    assert PrinterState().ams_slots == []
