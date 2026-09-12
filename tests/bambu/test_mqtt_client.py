import json

from app.bambu.mqtt_client import BambuMqttClient
from app.config import Settings


def make_settings(**over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="01P00A", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u")
    base.update(over)
    return Settings(**base)


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)


def test_topics_use_serial():
    c = BambuMqttClient(make_settings())
    assert c.report_topic == "device/01P00A/report"
    assert c.request_topic == "device/01P00A/request"


def test_handle_payload_updates_state():
    c = BambuMqttClient(make_settings())
    c.handle_payload(json.dumps({
        "print": {"gcode_state": "RUNNING", "mc_percent": 42, "subtask_name": "thing"}
    }).encode())
    assert c.state.state == "RUNNING"
    assert c.state.progress == 42
    assert c.state.file_name == "thing"


def test_handle_payload_ignores_non_print_messages():
    c = BambuMqttClient(make_settings())
    c.handle_payload(json.dumps({"info": {"command": "get_version"}}).encode())
    assert c.state.state is None


def test_handle_payload_survives_malformed_json():
    c = BambuMqttClient(make_settings())
    c.handle_payload(b"{not json")
    assert c.state.state is None, "a bad payload must not raise"


def test_handle_payload_survives_unexpected_shape():
    c = BambuMqttClient(make_settings())
    c.handle_payload(json.dumps({"print": "not-a-dict"}).encode())
    c.handle_payload(json.dumps(["a", "list"]).encode())
    assert c.state.state is None


def test_pushall_publishes_to_request_topic():
    c = BambuMqttClient(make_settings())
    c._client = FakeClient()
    c.request_pushall()
    topic, body = c._client.published[0]
    assert topic == "device/01P00A/request"
    assert body["pushing"]["command"] == "pushall", (
        "without pushall the printer only sends incremental reports"
    )


def test_pushall_increments_sequence_id():
    c = BambuMqttClient(make_settings())
    c._client = FakeClient()
    c.request_pushall()
    c.request_pushall()
    seen = [b["pushing"]["sequence_id"] for _, b in c._client.published]
    assert seen[0] != seen[1]


def test_pushall_without_client_is_a_noop():
    c = BambuMqttClient(make_settings())
    c.request_pushall()  # must not raise


def test_on_connect_subscribes_and_pushes_all():
    c = BambuMqttClient(make_settings())
    fake = FakeClient()
    c._client = fake
    c._on_connect(fake, None, None, 0)
    assert c.connected is True
    assert fake.subscribed == ["device/01P00A/report"]
    assert fake.published[0][1]["pushing"]["command"] == "pushall"


def test_on_connect_refused_does_not_mark_connected():
    c = BambuMqttClient(make_settings())
    fake = FakeClient()
    c._client = fake
    c._on_connect(fake, None, None, 5)
    assert c.connected is False
    assert fake.subscribed == []


def test_on_disconnect_clears_connected():
    c = BambuMqttClient(make_settings())
    c._client = FakeClient()
    c._on_connect(c._client, None, None, 0)
    c._on_disconnect(c._client, None, None, 7)
    assert c.connected is False
