import json
from datetime import UTC, datetime

from app.bambu.models import ImageFrame
from app.config import Settings
from app.detection.confirmation import Decision
from app.storage.session import PrintSession
from app.vision.analyzer import AnalysisResult
from app.vision.schemas import FailureAnalysis

STARTED = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


def make_settings(tmp_path, **over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u", data_dir=tmp_path)
    base.update(over)
    return Settings(**base)


def a_frame() -> ImageFrame:
    return ImageFrame(timestamp=STARTED, jpeg=b"\xff\xd8fake\xff\xd9")


def a_result() -> AnalysisResult:
    return AnalysisResult(
        analysis=FailureAnalysis(status="failure", confidence=0.93,
                                 failure_type="spaghetti", severity="high",
                                 explanation="Loose filament above the part."),
        input_tokens=1550, output_tokens=280, model="claude-sonnet-5",
        stop_reason="end_turn",
    )


def test_create_makes_session_directory(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "Mask_Final_v17", STARTED)
    assert s.directory.is_dir()
    assert (s.directory / "frames").is_dir()
    assert "mask_final_v17" in s.directory.name.lower()
    meta = json.loads((s.directory / "metadata.json").read_text())
    assert meta["file_name"] == "Mask_Final_v17"


def test_session_name_is_filesystem_safe(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "a/b c:d*?.3mf", STARTED)
    for ch in "/:*?":
        assert ch not in s.directory.name


def test_session_name_handles_missing_filename(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), None, STARTED)
    assert s.directory.is_dir()
    assert s.directory.name.endswith("_print")


def test_save_frame_writes_numbered_jpeg(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x", STARTED)
    p1 = s.save_frame(a_frame())
    p2 = s.save_frame(a_frame())
    assert p1.name == "0001.jpg"
    assert p2.name == "0002.jpg"
    assert p1.read_bytes() == b"\xff\xd8fake\xff\xd9"
    assert s.frame_count == 2


def test_save_frame_respects_disabled_saving(tmp_path):
    s = PrintSession.create(make_settings(tmp_path, save_frames=False), "x", STARTED)
    assert s.save_frame(a_frame()) is None


def test_save_frame_stops_at_max_frames(tmp_path):
    s = PrintSession.create(make_settings(tmp_path, max_session_frames=2), "x", STARTED)
    assert s.save_frame(a_frame()) is not None
    assert s.save_frame(a_frame()) is not None
    assert s.save_frame(a_frame()) is None, "storage must not grow without bound"


def test_record_appends_jsonl_with_usage(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x", STARTED)
    path = s.save_frame(a_frame())
    s.record(a_result(), Decision(alert=True, next_interval=45, reason="confirmed"), path)
    lines = (s.directory / "detections.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["failure_type"] == "spaghetti"
    assert row["confidence"] == 0.93
    assert row["alert"] is True
    assert row["input_tokens"] == 1550
    assert row["output_tokens"] == 280
    assert row["frame"] == "frames/0001.jpg"


def test_record_appends_rather_than_overwrites(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x", STARTED)
    d = Decision(alert=False, next_interval=45, reason="healthy")
    s.record(a_result(), d, None)
    s.record(a_result(), d, None)
    lines = (s.directory / "detections.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2


def test_record_handles_inconclusive_result(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x", STARTED)
    s.record(AnalysisResult(analysis=None, model="claude-sonnet-5"),
             Decision(alert=False, next_interval=45, reason="inconclusive"), None)
    row = json.loads((s.directory / "detections.jsonl").read_text().strip())
    assert row["status"] is None
    assert row["frame"] is None


def test_close_writes_totals(tmp_path):
    s = PrintSession.create(make_settings(tmp_path), "x", STARTED)
    s.record(a_result(), Decision(alert=True, next_interval=45, reason="c"), None)
    s.close("FINISH")
    meta = json.loads((s.directory / "metadata.json").read_text())
    assert meta["final_state"] == "FINISH"
    assert meta["alerts"] == 1
    assert meta["checks"] == 1
    assert meta["total_input_tokens"] == 1550
    assert meta["total_output_tokens"] == 280
    assert meta["ended_at"] is not None
    assert meta["vision_model"] == "claude-sonnet-5"
