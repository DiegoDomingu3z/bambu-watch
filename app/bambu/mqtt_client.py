"""MQTT/TLS client for the P1S on the local network.

paho-mqtt runs its own network thread and reconnects on its own; this class
only has to re-subscribe and re-issue pushall on each (re)connect. The
printer's certificate is self-signed, so verification is disabled -- this is
a LAN connection to a device authenticated by its access code.
"""

from __future__ import annotations

import json
import logging
import ssl
from itertools import count

import paho.mqtt.client as mqtt

from app.bambu.models import PrinterState
from app.config import Settings

logger = logging.getLogger(__name__)


class BambuMqttClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.state = PrinterState()
        self.report_topic = f"device/{settings.bambu_serial}/report"
        self.request_topic = f"device/{settings.bambu_serial}/request"
        self._sequence = count(1)
        self._connected = False
        self._client: mqtt.Client | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-watch-{self.settings.bambu_serial}",
        )
        client.username_pw_set("bblp", self.settings.bambu_access_code)
        client.tls_set(cert_reqs=ssl.CERT_NONE, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.tls_insecure_set(True)
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        self._client = client
        client.connect_async(self.settings.bambu_host, self.settings.bambu_mqtt_port, 60)
        client.loop_start()

    def disconnect(self) -> None:
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
        self._connected = False

    def request_pushall(self) -> None:
        """Force a full state snapshot. The P1S otherwise sends only
        incremental reports, so a freshly connected client can sit for
        minutes without learning that a print is already running."""
        if self._client is None:
            return
        payload = json.dumps(
            {"pushing": {"sequence_id": str(next(self._sequence)), "command": "pushall"}}
        )
        self._client.publish(self.request_topic, payload, qos=0)

    def handle_payload(self, payload: bytes) -> None:
        """Parse one report. Never raises: a malformed message from the
        printer must not take down the monitor."""
        try:
            message = json.loads(payload)
        except (ValueError, TypeError):
            logger.debug("ignoring unparseable MQTT payload")
            return

        if not isinstance(message, dict):
            return
        print_data = message.get("print")
        if not isinstance(print_data, dict):
            return

        try:
            self.state.apply_report(print_data)
        except Exception as exc:
            logger.warning("could not apply report: %s", exc)

    # --- paho callbacks (VERSION2 signatures) ---

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logger.error("MQTT connect refused: %s", reason_code)
            return
        self._connected = True
        logger.info("MQTT connected to %s", self.settings.bambu_host)
        client.subscribe(self.report_topic, qos=0)
        self.request_pushall()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self._connected = False
        logger.warning("MQTT disconnected: %s (paho will retry)", reason_code)

    def _on_message(self, client, userdata, message):
        self.handle_payload(message.payload)
