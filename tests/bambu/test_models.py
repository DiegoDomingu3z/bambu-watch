from datetime import UTC, datetime

from app.bambu.models import ImageFrame, PrinterState


def test_full_report_populates_state():
    s = PrinterState()
    s.apply_report({
        "gcode_state": "RUNNING",
        "mc_percent": 67,
        "layer_num": 845,
        "total_layer_num": 1261,
        "mc_remaining_time": 92,
        "subtask_name": "Mask_Final_v17",
        "gcode_file": "Metadata/plate_1.gcode",
    })
    assert s.state == "RUNNING"
    assert s.printing is True
    assert s.progress == 67
    assert s.layer == 845
    assert s.total_layers == 1261
    assert s.remaining_minutes == 92
    assert s.file_name == "Mask_Final_v17"


def test_incremental_report_preserves_absent_fields():
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 10, "layer_num": 5,
                    "subtask_name": "thing"})
    s.apply_report({"layer_num": 6})
    assert s.layer == 6
    assert s.progress == 10, "absent field must not be cleared"
    assert s.state == "RUNNING"
    assert s.file_name == "thing"


def test_subtask_name_preferred_over_gcode_file():
    s = PrinterState()
    s.apply_report({"gcode_file": "Metadata/plate_1.gcode"})
    assert s.file_name == "Metadata/plate_1.gcode", "falls back when no subtask_name"
    s.apply_report({"subtask_name": "Real_Name"})
    assert s.file_name == "Real_Name"


def test_blank_subtask_name_does_not_overwrite():
    s = PrinterState()
    s.apply_report({"subtask_name": "Real_Name"})
    s.apply_report({"subtask_name": ""})
    assert s.file_name == "Real_Name"


def test_printing_flag_tracks_gcode_state():
    s = PrinterState()
    for state, expected in [("RUNNING", True), ("PAUSE", False), ("FINISH", False),
                            ("FAILED", False), ("IDLE", False), ("PREPARE", False)]:
        s.apply_report({"gcode_state": state})
        assert s.printing is expected, state


def test_non_numeric_values_become_none_not_crash():
    s = PrinterState()
    s.apply_report({"mc_percent": "n/a", "layer_num": None})
    assert s.progress is None
    assert s.layer is None


def test_image_frame_holds_bytes():
    f = ImageFrame(timestamp=datetime.now(UTC), jpeg=b"\xff\xd8\xff\xd9")
    assert f.jpeg.startswith(b"\xff\xd8")
