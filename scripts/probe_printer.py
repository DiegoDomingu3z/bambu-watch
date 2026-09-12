"""Validate the undocumented P1S protocols against real hardware.

Run this once, with a populated .env, before trusting the service:

    .venv/bin/python scripts/probe_printer.py

Writes fixtures/live/report.json and fixtures/live/frame.jpg so the MQTT
report shape and the camera handshake can be verified. Prints the resolution
of the captured frame, which is what determines per-check token cost.

This script reads the access code from .env and never prints it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bambu.camera import P1SCamera
from app.bambu.mqtt_client import BambuMqttClient
from app.config import get_settings

OUT = Path("fixtures/live")
EXPECTED_KEYS = {
    "gcode_state",
    "mc_percent",
    "layer_num",
    "total_layer_num",
    "subtask_name",
    "mc_remaining_time",
}


async def probe_mqtt(settings) -> bool:
    print("--- MQTT ---")
    client = BambuMqttClient(settings)
    captured: list[dict] = []
    original = client.handle_payload

    def spy(payload: bytes) -> None:
        try:
            message = json.loads(payload)
            if isinstance(message, dict) and isinstance(message.get("print"), dict):
                captured.append(message)
        except ValueError:
            pass
        original(payload)

    client.handle_payload = spy  # type: ignore[method-assign]
    client.connect()

    for _ in range(30):
        await asyncio.sleep(1)
        if captured:
            break

    client.disconnect()

    if not captured:
        print("FAIL: no print reports in 30s.")
        print("  Check host, serial, and access code, and that port 8883 is open.")
        return False

    richest = max(captured, key=lambda m: len(m["print"]))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(richest, indent=2), encoding="utf-8")

    print(
        f"OK: {len(captured)} reports; richest has "
        f"{len(richest['print'])} keys -> {OUT / 'report.json'}"
    )
    print(
        f"  state={client.state.state} progress={client.state.progress} "
        f"layer={client.state.layer}/{client.state.total_layers} "
        f"file={client.state.file_name}"
    )

    missing = EXPECTED_KEYS - set(richest["print"])
    if missing:
        print(f"  NOTE: keys absent from this report: {sorted(missing)}")
        print("  That is expected when the printer is idle; re-run mid-print.")
    return True


async def probe_camera(settings) -> bool:
    print("--- Camera ---")
    try:
        frame = await P1SCamera(settings).capture()
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        print("  The port-6000 handshake is undocumented and may need correcting")
        print("  in app/bambu/camera.py (build_auth_packet / read_frame).")
        return False

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "frame.jpg"
    path.write_bytes(frame.jpeg)
    print(f"OK: {len(frame.jpeg)} bytes -> {path}")

    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
    except Exception as exc:
        print(f"  WARNING: captured bytes are not a readable image: {exc}")
        return False

    target = settings.frame_upload_width
    scaled_h = round(height * target / width)
    print(f"  resolution {width}x{height}, about {width * height // 750} tokens per frame")
    print(
        f"  downscaled to width {target}: about {target * scaled_h // 750} "
        "tokens per frame"
    )
    per_check = 3 * (target * scaled_h // 750)
    print(f"  three frames per check: about {per_check} image tokens")
    return True


async def main() -> int:
    try:
        settings = get_settings()
    except Exception as exc:
        print(f"Could not load settings: {exc}")
        print("Copy .env.example to .env and fill it in.")
        return 2

    print(f"Probing {settings.bambu_host} (serial {settings.bambu_serial})\n")
    mqtt_ok = await probe_mqtt(settings)
    print()
    camera_ok = await probe_camera(settings)

    print("\n--- Result ---")
    print(f"MQTT:   {'OK' if mqtt_ok else 'FAIL'}")
    print(f"Camera: {'OK' if camera_ok else 'FAIL'}")
    if mqtt_ok and camera_ok:
        print("\nBoth protocols verified. The service is safe to start.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
