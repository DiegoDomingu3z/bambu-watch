from datetime import UTC, datetime

import pytest

from app.bambu.models import ImageFrame, PrinterState
from app.bambu.slice_info import FilamentUsage, SliceInfo
from app.config import Settings
from app.detection.monitor import PrintMonitor
from app.storage.database import connect
from app.storage.prints import PrintRepository
from app.storage.records import OUTCOME_COMPLETED, OUTCOME_STOPPED
from app.vision.analyzer import AnalysisResult


def make_settings(tmp_path, **over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u", data_dir=tmp_path)
    base.update(over)
    return Settings(**base)


class StubPrinter:
    def __init__(self, state):
        self.state = state

    def connect(self): ...
    def disconnect(self): ...


class StubCamera:
    async def capture(self):
        return ImageFrame(timestamp=datetime.now(UTC), jpeg=b"\xff\xd8x\xff\xd9")


class StubAnalyzer:
    async def analyze(self, frames, state, interval):
        return AnalysisResult(analysis=None, input_tokens=100, output_tokens=20,
                              model="claude-sonnet-5")


class StubNotifier:
    async def send_failure(self, *a, **kw):
        return True


class StubFtp:
    def __init__(self, info=None, fail=False):
        self.info = info
        self.fail = fail
        self.calls = []

    async def fetch_slice_info(self, file_name):
        self.calls.append(file_name)
        return None if self.fail else self.info


def slice_info() -> SliceInfo:
    return SliceInfo(filaments=[
        FilamentUsage(1, "PLA", "#FF0000", 200.0, 66.0),
        FilamentUsage(2, "PLA", "#00FF00", 140.0, 47.2),
    ])


def running() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 10, "layer_num": 5,
                    "total_layer_num": 100, "subtask_name": "part"})
    return s


def preparing() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "PREPARE", "subtask_name": "part",
                    "total_layer_num": 100})
    return s


def build(tmp_path, state, ftp=None, **over):
    settings = make_settings(tmp_path, **over)
    repo = PrintRepository(connect(settings.db_path), settings)
    monitor = PrintMonitor(settings, StubPrinter(state), StubCamera(),
                           StubAnalyzer(), StubNotifier(),
                           repository=repo, ftp=ftp)
    return monitor, repo


async def settle(monitor, passes=4):
    for _ in range(passes):
        await monitor.run_once()


async def test_prepare_state_is_reported_and_does_not_capture(tmp_path):
    monitor, _ = build(tmp_path, preparing())
    assert await monitor.run_once() == "preparing"
    assert monitor.session is None, "no session until the print actually runs"


async def test_prepare_fetches_slice_info(tmp_path):
    ftp = StubFtp(slice_info())
    monitor, _ = build(tmp_path, preparing(), ftp=ftp)
    await settle(monitor, 6)
    assert ftp.calls == ["part"], "exactly one fetch per print"
    assert monitor.slice_info is not None
    assert monitor.slice_info.total_grams == pytest.approx(340.0)


async def test_no_fetch_is_attempted_while_running(tmp_path):
    ftp = StubFtp(slice_info())
    monitor, _ = build(tmp_path, running(), ftp=ftp)
    await settle(monitor, 4)
    assert ftp.calls == [], "FTPS must never be touched during RUNNING"


async def test_print_row_is_inserted_at_start(tmp_path):
    monitor, repo = build(tmp_path, running())
    await monitor.run_once()
    row = repo.get(monitor.session.id)
    assert row is not None
    assert row["outcome"] == "running"
    assert row["file_name"] == "part"
    assert row["ended_at"] is None


async def test_completed_print_is_finalized_with_material(tmp_path):
    state = preparing()
    ftp = StubFtp(slice_info())
    monitor, repo = build(tmp_path, state, ftp=ftp)
    await settle(monitor, 3)
    assert monitor.slice_info is not None, "precondition: fetched during PREPARE"

    state.apply_report({"gcode_state": "RUNNING", "mc_percent": 50, "layer_num": 50})
    await monitor.run_once()
    print_id = monitor.session.id

    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100, "layer_num": 100})
    await monitor.run_once()

    row = repo.get(print_id)
    assert row["outcome"] == OUTCOME_COMPLETED
    assert row["planned_grams"] == pytest.approx(340.0)
    assert row["planned_meters"] == pytest.approx(113.2)
    assert row["planned_cost"] == pytest.approx(4.42)
    assert row["consumed_grams"] == pytest.approx(340.0)
    assert row["consumed_cost"] == pytest.approx(4.42)
    assert row["consumption_method"] == "complete"
    assert row["slice_info_source"] == "ftps"
    assert len(repo.filaments(print_id)) == 2


async def test_stopped_print_records_estimated_consumption(tmp_path):
    state = preparing()
    monitor, repo = build(tmp_path, state, ftp=StubFtp(slice_info()))
    await settle(monitor, 3)

    state.apply_report({"gcode_state": "RUNNING", "mc_percent": 50,
                        "layer_num": 50, "total_layer_num": 100})
    await monitor.run_once()
    print_id = monitor.session.id

    state.apply_report({"gcode_state": "IDLE"})
    await monitor.run_once()

    row = repo.get(print_id)
    assert row["outcome"] == OUTCOME_STOPPED
    assert row["consumed_grams"] == pytest.approx(170.0)
    assert row["consumed_cost"] == pytest.approx(2.21)
    assert row["consumption_method"] == "layer_fraction"
    assert row["planned_grams"] == pytest.approx(340.0), "planned is unchanged"


async def test_post_print_fetch_when_prepare_was_missed(tmp_path):
    state = running()
    ftp = StubFtp(slice_info())
    monitor, repo = build(tmp_path, state, ftp=ftp)
    await monitor.run_once()
    print_id = monitor.session.id
    assert ftp.calls == [], "nothing fetched while printing"

    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100, "layer_num": 100})
    await monitor.run_once()
    assert ftp.calls == ["part"], "the print is over, so fetching is safe"
    assert repo.get(print_id)["planned_grams"] == pytest.approx(340.0)


async def test_unavailable_slice_info_still_records_the_print(tmp_path):
    state = running()
    monitor, repo = build(tmp_path, state, ftp=StubFtp(fail=True))
    await settle(monitor, 3)
    print_id = monitor.session.id

    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100, "layer_num": 100})
    await monitor.run_once()

    row = repo.get(print_id)
    assert row["outcome"] == OUTCOME_COMPLETED
    assert row["planned_grams"] is None
    assert row["consumed_grams"] is None
    assert row["slice_info_source"] == "unavailable"
    assert row["checks"] == 1, "monitoring stats are still captured"


async def test_no_ftp_client_at_all_still_records(tmp_path):
    state = running()
    monitor, repo = build(tmp_path, state, ftp=None)
    await monitor.run_once()
    print_id = monitor.session.id
    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100, "layer_num": 100})
    await monitor.run_once()
    assert repo.get(print_id)["outcome"] == OUTCOME_COMPLETED


async def test_disabled_switch_attempts_nothing(tmp_path):
    state = preparing()
    ftp = StubFtp(slice_info())
    monitor, repo = build(tmp_path, state, ftp=ftp, enable_slice_fetch=False)
    await settle(monitor, 3)
    assert ftp.calls == []

    state.apply_report({"gcode_state": "RUNNING"})
    await monitor.run_once()
    print_id = monitor.session.id
    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100, "layer_num": 100})
    await monitor.run_once()
    assert ftp.calls == [], "the master switch must gate the post-print fetch too"
    assert repo.get(print_id)["slice_info_source"] == "disabled"


async def test_monitor_works_without_a_repository(tmp_path):
    settings = make_settings(tmp_path)
    state = running()
    monitor = PrintMonitor(settings, StubPrinter(state), StubCamera(),
                           StubAnalyzer(), StubNotifier())
    assert await monitor.run_once() == "warmup"
    state.apply_report({"gcode_state": "FINISH"})
    assert await monitor.run_once() == "idle", "the database is optional"


async def test_new_print_refetches_slice_info(tmp_path):
    state = preparing()
    ftp = StubFtp(slice_info())
    monitor, _ = build(tmp_path, state, ftp=ftp)
    await settle(monitor, 3)
    state.apply_report({"gcode_state": "RUNNING"})
    await monitor.run_once()
    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100})
    await monitor.run_once()
    assert monitor.slice_info is None, "slice state must reset between prints"

    state.apply_report({"gcode_state": "PREPARE", "subtask_name": "second"})
    await settle(monitor, 3)
    assert ftp.calls[-1] == "second", "a new print needs its own slice info"


async def test_fetch_is_cancelled_when_the_printer_starts_printing(tmp_path):
    """The load-bearing safety behaviour: a transfer in flight when PREPARE
    ends is cancelled rather than continuing into a live print."""
    import asyncio

    state = preparing()
    started = asyncio.Event()

    class SlowFtp:
        def __init__(self):
            self.calls = []
            self.completed = False

        async def fetch_slice_info(self, file_name):
            self.calls.append(file_name)
            started.set()
            await asyncio.sleep(10)
            self.completed = True
            return slice_info()

    ftp = SlowFtp()
    monitor, _ = build(tmp_path, state, ftp=ftp)

    await monitor.run_once()
    await asyncio.wait_for(started.wait(), timeout=1)
    assert monitor._slice_task is not None

    state.apply_report({"gcode_state": "RUNNING", "mc_percent": 1, "layer_num": 1})
    await monitor.run_once()

    assert monitor._slice_task is None, "the in-flight fetch must be cancelled"
    assert ftp.completed is False, "it must not have run to completion"


async def test_filament_rows_carry_colours(tmp_path):
    state = preparing()
    monitor, repo = build(tmp_path, state, ftp=StubFtp(slice_info()))
    await settle(monitor, 3)
    state.apply_report({"gcode_state": "RUNNING"})
    await monitor.run_once()
    print_id = monitor.session.id
    state.apply_report({"gcode_state": "FINISH", "mc_percent": 100, "layer_num": 100})
    await monitor.run_once()

    rows = repo.filaments(print_id)
    assert {r["color"] for r in rows} == {"#FF0000", "#00FF00"}
    assert sum(r["used_grams"] for r in rows) == pytest.approx(340.0)


async def test_history_failure_does_not_end_the_session(tmp_path):
    state = running()
    monitor, _ = build(tmp_path, state)

    class Boom:
        def insert_running(self, *a, **kw):
            raise RuntimeError("disk full")

        def finalize(self, *a, **kw):
            raise RuntimeError("disk full")

        def set_slice_source(self, *a, **kw): ...
        def save_filaments(self, *a, **kw): ...

    monitor.repository = Boom()
    assert await monitor.run_once() == "warmup"
    state.apply_report({"gcode_state": "FINISH"})
    assert await monitor.run_once() == "idle", (
        "a database failure must never end monitoring"
    )
