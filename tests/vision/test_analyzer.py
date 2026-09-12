import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.vision.analyzer import AnalysisResult, VisionAnalyzer, downscale_jpeg
from app.vision.prompts import render_context
from app.vision.schemas import FailureAnalysis


def make_settings(**over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u")
    base.update(over)
    return Settings(**base)


def jpeg_of(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def frames(n=3) -> list[ImageFrame]:
    t0 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)
    return [ImageFrame(timestamp=t0 + timedelta(seconds=45 * i), jpeg=jpeg_of(1280, 720))
            for i in range(n)]


def state() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 67, "layer_num": 845,
                    "total_layer_num": 1261, "subtask_name": "Mask_Final_v17"})
    return s


# --- downscaling ---

def test_downscale_reduces_width_and_keeps_aspect():
    out = downscale_jpeg(jpeg_of(1280, 720), 640)
    assert Image.open(io.BytesIO(out)).size == (640, 360)


def test_downscale_leaves_smaller_image_alone():
    src = jpeg_of(320, 180)
    assert downscale_jpeg(src, 640) == src


def test_downscale_returns_original_on_undecodable_bytes():
    assert downscale_jpeg(b"not a jpeg", 640) == b"not a jpeg"


def test_downscale_actually_shrinks_payload():
    src = jpeg_of(1280, 720)
    assert len(downscale_jpeg(src, 640)) < len(src), "cost saving depends on this"


# --- prompt context ---

def test_context_includes_printer_metadata():
    text = render_context(state(), count=3, interval=45)
    assert "67%" in text
    assert "845" in text
    assert "1261" in text
    assert "Image 3" in text


def test_context_labels_newest_frame_as_current():
    text = render_context(state(), count=3, interval=45)
    assert "Image 3: current" in text
    assert "Image 1: approximately 90 seconds ago" in text


def test_context_tolerates_empty_state():
    text = render_context(PrinterState(), count=1, interval=45)
    assert "Bambu Lab P1S" in text
    assert "Image 1: current" in text


# --- analyzer, with a stubbed client so no test spends credit ---

class StubMessages:
    def __init__(self, parsed, stop_reason="end_turn", usage=(4200, 300)):
        self.parsed = parsed
        self._stop_reason = stop_reason
        self._usage = usage
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed, stop_reason, usage = self.parsed, self._stop_reason, self._usage

        class Usage:
            input_tokens = usage[0]
            output_tokens = usage[1]

        class Resp:
            pass

        r = Resp()
        r.parsed_output = parsed
        r.stop_reason = stop_reason
        r.usage = Usage()
        return r


class StubClient:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.messages = StubMessages(parsed, stop_reason)


@pytest.fixture
def healthy() -> FailureAnalysis:
    return FailureAnalysis(status="healthy", confidence=0.97, failure_type="none",
                           severity="none", explanation="Looks fine.")


async def test_analyze_returns_parsed_analysis_and_usage(healthy):
    a = VisionAnalyzer(make_settings(), client=StubClient(healthy))
    res = await a.analyze(frames(), state(), interval=45)
    assert isinstance(res, AnalysisResult)
    assert res.analysis is healthy
    assert res.input_tokens == 4200
    assert res.output_tokens == 300
    assert res.model == "claude-sonnet-5"


async def test_analyze_sends_one_image_block_per_frame(healthy):
    client = StubClient(healthy)
    a = VisionAnalyzer(make_settings(), client=client)
    await a.analyze(frames(3), state(), interval=45)
    content = client.messages.calls[0]["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 3
    assert images[0]["source"]["media_type"] == "image/jpeg"
    assert content[-1]["type"] == "text", "metadata goes after the images"


async def test_analyze_requests_adaptive_thinking_and_low_effort(healthy):
    client = StubClient(healthy)
    a = VisionAnalyzer(make_settings(), client=client)
    await a.analyze(frames(), state(), interval=45)
    kwargs = client.messages.calls[0]
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["output_format"] is FailureAnalysis
    assert "budget_tokens" not in str(kwargs), "rejected with 400 on this model"


async def test_analyze_uploads_downscaled_frames(healthy):
    import base64
    client = StubClient(healthy)
    a = VisionAnalyzer(make_settings(), client=client)
    await a.analyze(frames(1), state(), interval=45)
    content = client.messages.calls[0]["messages"][0]["content"]
    data = base64.standard_b64decode(content[0]["source"]["data"])
    assert Image.open(io.BytesIO(data)).size == (640, 360)


async def test_analyze_treats_refusal_as_inconclusive(healthy):
    a = VisionAnalyzer(make_settings(), client=StubClient(healthy, stop_reason="refusal"))
    res = await a.analyze(frames(), state(), interval=45)
    assert res.analysis is None, "a refusal must never read as a detection"
    assert res.stop_reason == "refusal"


async def test_analyze_handles_none_parsed_output():
    a = VisionAnalyzer(make_settings(), client=StubClient(None))
    res = await a.analyze(frames(), state(), interval=45)
    assert res.analysis is None


async def test_analyze_returns_inconclusive_on_api_error():
    class Boom:
        class messages:
            @staticmethod
            async def parse(**kwargs):
                raise RuntimeError("connection reset")

    a = VisionAnalyzer(make_settings(), client=Boom())
    res = await a.analyze(frames(), state(), interval=45)
    assert res.analysis is None, "an API error is a skipped check, not a crash"
    assert res.model == "claude-sonnet-5"
