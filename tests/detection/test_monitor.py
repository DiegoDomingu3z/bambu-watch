import json
from datetime import UTC, datetime

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.detection.monitor import PrintMonitor
from app.vision.analyzer import AnalysisResult
from app.vision.schemas import FailureAnalysis


def make_settings(tmp_path, **over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u", data_dir=tmp_path)
    base.update(over)
    return Settings(**base)


class StubPrinter:
    def __init__(self, state: PrinterState):
        self.state = state
        self.connected = True
        self.connects = 0
        self.disconnects = 0

    def connect(self):
        self.connects += 1

    def disconnect(self):
        self.disconnects += 1


class StubCamera:
    def __init__(self, fail=False):
        self.fail = fail
        self.captures = 0

    async def capture(self) -> ImageFrame:
        self.captures += 1
        if self.fail:
            raise OSError("camera unreachable")
        return ImageFrame(timestamp=datetime.now(UTC), jpeg=b"\xff\xd8x\xff\xd9")


class StubAnalyzer:
    def __init__(self, *results):
        self.queue = list(results)
        self.calls = 0
        self.intervals = []

    async def analyze(self, frames, state, interval) -> AnalysisResult:
        self.calls += 1
        self.intervals.append(interval)
        if self.queue:
            return self.queue.pop(0)
        return AnalysisResult(analysis=None, model="m")


class StubNotifier:
    def __init__(self):
        self.sent = []

    async def send_failure(self, analysis, frame, state) -> bool:
        self.sent.append(analysis)
        return True


def running_state() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 10, "layer_num": 5,
                    "total_layer_num": 100, "subtask_name": "part"})
    return s


def result(status="failure", confidence=0.93, failure_type="spaghetti") -> AnalysisResult:
    return AnalysisResult(
        analysis=FailureAnalysis(status=status, confidence=confidence,
                                 failure_type=failure_type, severity="high",
                                 explanation="e"),
        input_tokens=100, output_tokens=20, model="claude-sonnet-5",
        stop_reason="end_turn",
    )


def build(tmp_path, printer, camera, analyzer, notifier, **over) -> PrintMonitor:
    return PrintMonitor(make_settings(tmp_path, **over), printer, camera, analyzer,
                        notifier)


async def test_idle_printer_does_not_capture(tmp_path):
    cam = StubCamera()
    m = build(tmp_path, StubPrinter(PrinterState()), cam, StubAnalyzer(), StubNotifier())
    assert await m.run_once() == "idle"
    assert cam.captures == 0, "must not touch the camera when not printing"


async def test_warmup_captures_but_does_not_analyze(tmp_path):
    an = StubAnalyzer()
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(), an, StubNotifier())
    assert await m.run_once() == "warmup"
    assert await m.run_once() == "warmup"
    assert an.calls == 0, "no analysis until the buffer holds 3 frames"
    assert await m.run_once() == "analyzed"
    assert an.calls == 1


async def test_capture_failure_is_skipped_not_fatal(tmp_path):
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(fail=True),
              StubAnalyzer(), StubNotifier())
    assert await m.run_once() == "capture_failed"
    assert m.next_interval == 45, "a camera failure must not change cadence"


async def test_two_matching_failures_alert_once(tmp_path):
    an = StubAnalyzer(result(), result(), result(), result())
    note = StubNotifier()
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(), an, note)
    for _ in range(2):
        await m.run_once()
    assert await m.run_once() == "analyzed"  # buffer fills, first detection
    assert m.next_interval == 10
    assert await m.run_once() == "alerted"
    assert len(note.sent) == 1
    assert note.sent[0].failure_type == "spaghetti"


async def test_alert_persists_detection_and_frame(tmp_path):
    an = StubAnalyzer(result(), result())
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(), an, StubNotifier())
    for _ in range(4):
        await m.run_once()
    rows = [json.loads(x) for x in
            (m.session.directory / "detections.jsonl").read_text().strip().splitlines()]
    assert any(r["alert"] for r in rows)
    assert rows[0]["frame"] == "frames/0001.jpg"
    assert rows[0]["input_tokens"] == 100


async def test_paused_printer_suspends_analysis(tmp_path):
    state = running_state()
    an, cam = StubAnalyzer(), StubCamera()
    m = build(tmp_path, StubPrinter(state), cam, an, StubNotifier())
    state.apply_report({"gcode_state": "PAUSE"})
    assert await m.run_once() == "paused"
    assert cam.captures == 0
    assert an.calls == 0


async def test_pause_keeps_session_open(tmp_path):
    state = running_state()
    m = build(tmp_path, StubPrinter(state), StubCamera(), StubAnalyzer(), StubNotifier())
    await m.run_once()
    assert m.session is not None
    state.apply_report({"gcode_state": "PAUSE"})
    await m.run_once()
    assert m.session is not None, "a pause may be followed by a resume"


async def test_session_created_and_closed_across_print(tmp_path):
    state = running_state()
    m = build(tmp_path, StubPrinter(state), StubCamera(), StubAnalyzer(), StubNotifier())
    await m.run_once()
    assert m.session is not None
    directory = m.session.directory
    state.apply_report({"gcode_state": "FINISH"})
    await m.run_once()
    assert m.session is None, "session must close when the print ends"
    meta = json.loads((directory / "metadata.json").read_text())
    assert meta["final_state"] == "FINISH"


async def test_new_print_starts_new_session(tmp_path):
    state = running_state()
    m = build(tmp_path, StubPrinter(state), StubCamera(), StubAnalyzer(), StubNotifier())
    await m.run_once()
    first = m.session.directory
    state.apply_report({"gcode_state": "FINISH"})
    await m.run_once()
    state.apply_report({"gcode_state": "RUNNING", "subtask_name": "second"})
    await m.run_once()
    assert m.session is not None
    assert m.session.directory != first


async def test_buffer_cleared_between_sessions(tmp_path):
    state = running_state()
    an = StubAnalyzer()
    m = build(tmp_path, StubPrinter(state), StubCamera(), an, StubNotifier())
    for _ in range(2):
        await m.run_once()
    state.apply_report({"gcode_state": "FINISH"})
    await m.run_once()
    state.apply_report({"gcode_state": "RUNNING", "subtask_name": "second"})
    assert await m.run_once() == "warmup", "stale frames must not seed a new print"
    assert an.calls == 0


async def test_interval_resets_to_normal_on_new_session(tmp_path):
    state = running_state()
    an = StubAnalyzer(result(), result(), result())
    m = build(tmp_path, StubPrinter(state), StubCamera(), an, StubNotifier())
    for _ in range(3):
        await m.run_once()
    assert m.next_interval == 10, "precondition: suspicious"
    state.apply_report({"gcode_state": "FINISH"})
    await m.run_once()
    assert m.next_interval == 45


async def test_notification_failure_does_not_raise(tmp_path):
    class Boom:
        async def send_failure(self, analysis, frame, state):
            raise RuntimeError("discord down")

    an = StubAnalyzer(result(), result(), result(), result())
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(), an, Boom())
    for _ in range(3):
        await m.run_once()
    assert await m.run_once() == "alerted", "alert path must survive a notifier error"


async def test_inconclusive_analysis_records_but_does_not_alert(tmp_path):
    an = StubAnalyzer()  # yields analysis=None
    note = StubNotifier()
    m = build(tmp_path, StubPrinter(running_state()), StubCamera(), an, note)
    for _ in range(3):
        await m.run_once()
    assert note.sent == []
    row = json.loads(
        (m.session.directory / "detections.jsonl").read_text().strip().splitlines()[0]
    )
    assert row["status"] is None
