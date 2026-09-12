from datetime import UTC, datetime

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.notifications.discord import (
    DiscordNotifier,
    MaterialSummary,
    PrintFinished,
    PrintStarted,
)
from app.vision.schemas import FailureAnalysis

WEBHOOK = "https://discord.com/api/webhooks/1/tok"


def make_settings(**over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url=WEBHOOK)
    base.update(over)
    return Settings(**base)


def analysis() -> FailureAnalysis:
    return FailureAnalysis(status="failure", confidence=0.94,
                           failure_type="detached_support", severity="high",
                           explanation="The front-right support appears separated.")


def state() -> PrinterState:
    s = PrinterState()
    s.apply_report({"gcode_state": "RUNNING", "mc_percent": 71, "layer_num": 883,
                    "total_layer_num": 1240, "subtask_name": "Mask_Final_v17.3mf"})
    return s


def a_frame() -> ImageFrame:
    return ImageFrame(timestamp=datetime(2026, 9, 12, tzinfo=UTC),
                      jpeg=b"\xff\xd8x\xff\xd9")


class StubResponse:
    def __init__(self, status_code=204):
        self.status_code = status_code
        self.text = ""


class StubClient:
    def __init__(self, status_code=204, boom=False):
        self.status_code = status_code
        self.boom = boom
        self.calls = []

    async def post(self, url, **kwargs):
        if self.boom:
            raise RuntimeError("network down")
        self.calls.append((url, kwargs))
        return StubResponse(self.status_code)


def test_message_contains_actionable_detail():
    text = DiscordNotifier.format_message(analysis(), state())
    assert "detached support" in text.lower()
    assert "94%" in text
    assert "71%" in text
    assert "883" in text
    assert "1240" in text
    assert "Mask_Final_v17.3mf" in text
    assert "Handy" in text, "must tell the user where to act"


def test_message_states_it_does_not_pause():
    text = DiscordNotifier.format_message(analysis(), state())
    assert "does not pause" in text.lower()


def test_message_handles_missing_metadata():
    text = DiscordNotifier.format_message(analysis(), PrinterState())
    assert "detached support" in text.lower()
    assert "Printer: P1S" in text


def test_message_omits_total_when_unknown():
    s = PrinterState()
    s.apply_report({"layer_num": 12})
    text = DiscordNotifier.format_message(analysis(), s)
    assert "Layer: 12" in text
    assert "Layer: 12 /" not in text


async def test_send_posts_multipart_with_image():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), a_frame(), state()) is True
    url, kwargs = client.calls[0]
    assert url == WEBHOOK
    assert "files" in kwargs, "the image is the evidence; it must be attached"
    assert kwargs["files"]["file"][0] == "failure.jpg"
    assert kwargs["files"]["file"][2] == "image/jpeg"


async def test_send_without_frame_posts_json_only():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), None, state()) is True
    _, kwargs = client.calls[0]
    assert "files" not in kwargs
    assert "json" in kwargs


async def test_send_returns_false_on_http_error():
    n = DiscordNotifier(make_settings(), client=StubClient(status_code=500))
    assert await n.send_failure(analysis(), a_frame(), state()) is False


async def test_send_returns_false_on_rate_limit():
    n = DiscordNotifier(make_settings(), client=StubClient(status_code=429))
    assert await n.send_failure(analysis(), a_frame(), state()) is False


async def test_send_returns_false_on_network_exception():
    n = DiscordNotifier(make_settings(), client=StubClient(boom=True))
    assert await n.send_failure(analysis(), a_frame(), state()) is False, (
        "a failed notification must not crash the monitor"
    )


async def test_injected_client_is_not_closed():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    await n.send_failure(analysis(), None, state())
    await n.send_failure(analysis(), None, state())
    assert len(client.calls) == 2, "a reused client must survive the first send"


# --- material lines ---

def material(**over) -> MaterialSummary:
    base = dict(planned_grams=340.0, consumed_grams=240.0,
                consumed_cost=3.12, estimated=True)
    base.update(over)
    return MaterialSummary(**base)


def test_message_includes_material_when_known():
    text = DiscordNotifier.format_message(analysis(), state(), material())
    assert "240" in text
    assert "340" in text
    assert "3.12" in text


def test_estimated_material_says_so():
    text = DiscordNotifier.format_message(analysis(), state(), material())
    assert "estimated" in text.lower(), (
        "a layer-fraction figure must never look measured"
    )


def test_exact_material_does_not_say_estimated():
    text = DiscordNotifier.format_message(analysis(), state(),
                                          material(estimated=False))
    assert "estimated" not in text.lower()
    assert "3.12" in text


def test_message_omits_material_when_absent():
    text = DiscordNotifier.format_message(analysis(), state(), None)
    assert "Material" not in text
    assert "0g" not in text, "unknown must never render as zero"


def test_message_omits_material_when_grams_unknown():
    text = DiscordNotifier.format_message(
        analysis(), state(), material(consumed_grams=None, consumed_cost=None))
    assert "Material" not in text


def test_material_without_cost_still_renders_grams():
    text = DiscordNotifier.format_message(
        analysis(), state(), material(consumed_cost=None))
    assert "240" in text
    assert "estimated" in text.lower()


def test_material_without_planned_total_still_renders():
    text = DiscordNotifier.format_message(
        analysis(), state(), material(planned_grams=None))
    assert "240" in text
    assert "planned" not in text


async def test_send_passes_material_through():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), a_frame(), state(), material()) is True
    body = client.calls[0][1]["data"]["payload_json"]
    assert "240" in body
    assert "estimated" in body.lower()


async def test_send_without_material_still_works():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_failure(analysis(), a_frame(), state()) is True


# --- print start and finish notifications ---

def test_started_message_has_the_essentials():
    text = DiscordNotifier.format_started(PrintStarted(
        file_name="01_Platform_AMS", total_layers=300,
        planned_grams=340.0, planned_cost=4.42,
        filaments=[("PLA", "#FF0000"), ("PLA", "#00FF00")]))
    assert "PRINT STARTED" in text
    assert "01_Platform_AMS" in text
    assert "300" in text
    assert "340g" in text
    assert "4.42" in text
    assert "#FF0000" in text and "#00FF00" in text
    assert "never pauses" in text


def test_started_message_without_material():
    text = DiscordNotifier.format_started(PrintStarted(file_name="thing"))
    assert "PRINT STARTED" in text
    assert "thing" in text
    assert "Material" not in text, "no figures means no material line"
    assert "Colours" not in text


def test_started_message_without_a_name():
    text = DiscordNotifier.format_started(PrintStarted())
    assert "unknown" in text


def test_finished_message_for_a_completed_print():
    text = DiscordNotifier.format_finished(PrintFinished(
        file_name="01_Platform_AMS", outcome="completed",
        duration_seconds=3 * 3600 + 42 * 60, layer_at_end=300, total_layers=300,
        consumed_grams=340.0, consumed_cost=4.42, estimated=False,
        checks=296, alerts=0, monitoring_cost=1.38))
    assert "PRINT FINISHED" in text
    assert "3h 42m" in text
    assert "used" in text
    assert "wasted" not in text
    assert "340g" in text
    assert "estimated" not in text.lower()
    assert "296" in text
    assert "1.38" in text


def test_finished_message_for_a_stopped_print_says_wasted_and_estimated():
    text = DiscordNotifier.format_finished(PrintFinished(
        file_name="x", outcome="stopped", duration_seconds=1800,
        consumed_grams=170.0, consumed_cost=2.21, estimated=True,
        checks=40, alerts=1))
    assert "PRINT STOPPED" in text
    assert "wasted" in text
    assert "estimated" in text.lower()
    assert "30m" in text


def test_finished_message_for_a_failed_print():
    text = DiscordNotifier.format_finished(PrintFinished(outcome="failed"))
    assert "PRINT FAILED" in text


def test_finished_message_for_an_unknown_outcome():
    text = DiscordNotifier.format_finished(PrintFinished(outcome="unknown"))
    assert "PRINT ENDED" in text
    assert "unknown" in text


def test_finished_message_omits_unknown_figures():
    text = DiscordNotifier.format_finished(PrintFinished(outcome="completed"))
    assert "Material" not in text
    assert "Duration" not in text
    assert "Monitoring cost" not in text
    assert "0g" not in text


def test_duration_formats_minutes_only_under_an_hour():
    text = DiscordNotifier.format_finished(
        PrintFinished(outcome="completed", duration_seconds=125))
    assert "2m" in text
    assert "0h" not in text


async def test_send_print_started_posts_text_only():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_print_started(PrintStarted(file_name="x")) is True
    _, kwargs = client.calls[0]
    assert "json" in kwargs
    assert "files" not in kwargs


async def test_send_print_finished_attaches_the_frame():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    ok = await n.send_print_finished(
        PrintFinished(outcome="completed"), a_frame())
    assert ok is True
    _, kwargs = client.calls[0]
    assert kwargs["files"]["file"][0] == "finished.jpg"


async def test_send_print_finished_without_a_frame():
    client = StubClient()
    n = DiscordNotifier(make_settings(), client=client)
    assert await n.send_print_finished(PrintFinished(outcome="completed")) is True
    assert "files" not in client.calls[0][1]


async def test_start_and_finish_survive_a_network_failure():
    n = DiscordNotifier(make_settings(), client=StubClient(boom=True))
    assert await n.send_print_started(PrintStarted()) is False
    assert await n.send_print_finished(PrintFinished()) is False
