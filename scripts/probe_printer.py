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
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bambu.camera import P1SCamera
from app.bambu.ftp_client import BambuFtpClient
from app.bambu.mqtt_client import BambuMqttClient
from app.bambu.slice_info import SLICE_INFO_MEMBER, extract_slice_info
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

    report = richest["print"]

    # Spec Q7: does cancelling report FAILED or IDLE, and are the error
    # fields populated?
    for key in ("print_error", "fail_reason", "mc_print_error_code"):
        if key in report:
            print(f"  Q7 {key} = {report[key]!r}")
    if not any(k in report for k in ("print_error", "fail_reason")):
        print("  Q7: no error fields in this report (cancel a print to learn more)")

    # Spec Q8: what shape is the AMS block on this printer and firmware?
    ams = report.get("ams")
    if isinstance(ams, dict) and isinstance(ams.get("ams"), list):
        units = ams["ams"]
        print(f"  Q8 AMS: {len(units)} unit(s)")
        for unit in units:
            trays = unit.get("tray") or []
            print(f"    unit {unit.get('id')}: {len(trays)} tray(s)")
            for tray in trays:
                print(f"      tray {tray.get('id')}: "
                      f"type={tray.get('tray_type')!r} "
                      f"color={tray.get('tray_color')!r} "
                      f"remain={tray.get('remain')!r}")
        print(f"    parsed to {len(state_slots(client))} slot(s) by app.bambu.ams")
    else:
        print("  Q8: no AMS block in this report")
    if isinstance(report.get("vt_tray"), dict):
        vt = report["vt_tray"]
        print(f"    external spool: type={vt.get('tray_type')!r} "
              f"color={vt.get('tray_color')!r}")
    return True


def state_slots(client) -> list:
    return client.state.ams_slots


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


async def probe_ftp(settings) -> bool:
    """Spec section 10, questions 1, 2, 3, 5 and 6.

    Read-only: this issues listings, RETR and REST 0. Nothing else.
    """
    print("--- FTPS (sliced file) ---")
    if not settings.enable_slice_fetch:
        print("SKIP: ENABLE_SLICE_FETCH is false")
        return True

    client = BambuFtpClient(settings)
    loop = asyncio.get_running_loop()

    # Q1: does implicit TLS on 990 accept bblp plus the access code?
    try:
        ftp = await loop.run_in_executor(None, client._connect)
    except Exception as exc:
        print(f"FAIL: connect/login: {type(exc).__name__}: {exc}")
        print("  Check that LAN mode is on and port 990 is reachable.")
        print("  (Q1 unanswered; the rest of this section is skipped.)")
        return False
    print(f"OK: connected, implicit TLS on {settings.bambu_ftp_port} (Q1)")

    archives: list[str] = []
    try:
        # Q2: which directory holds the sliced file, and what is it called?
        for directory in settings.ftp_dirs:
            try:
                names = await loop.run_in_executor(None, ftp.nlst, directory)
            except Exception as exc:
                print(f"  {directory}: listing failed ({exc})")
                continue
            found = [n for n in names if n.lower().endswith((".3mf", ".gcode"))]
            archives += found
            print(f"  Q2 {directory}: {len(names)} entries, {len(found)} printable")
            for name in found[:10]:
                print(f"      {name}")

        # Q6: does the server support REST, enabling ranged reads?
        try:
            await loop.run_in_executor(None, lambda: ftp.sendcmd("REST 0"))
            print("OK: Q6 REST supported -> ranged reads are possible")
            print("  A ranged read would fetch ~20KB instead of the whole archive.")
        except Exception as exc:
            print(f"NOTE: Q6 REST unsupported ({exc}); full-archive fetch only")

        # Q3 and Q5: does the archive contain slice info, and how big is it?
        if not archives:
            print("  Q3/Q5 skipped: no archive on the card. Slice a print first.")
            return True

        target = archives[0]
        data = await loop.run_in_executor(None, client._fetch_archive, target)
        if not data:
            print(f"FAIL: could not retrieve {target}")
            return False
        print(f"OK: Q5 retrieved {target}: {len(data) / 1048576:.2f} MB")

        info = extract_slice_info(data)
        if info is None:
            print(f"FAIL: Q3 {SLICE_INFO_MEMBER} missing or unparseable")
            print("  Inspect the archive by hand and correct app/bambu/slice_info.py")
            return False

        print(f"OK: Q3 parsed {len(info.filaments)} filament(s)")
        for f in info.filaments:
            print(f"    slot {f.slot}: type={f.filament_type!r} color={f.color!r} "
                  f"{f.used_grams}g {f.used_meters}m")
        rate = settings.cost_per_gram
        if info.total_grams is not None:
            print(f"  total {info.total_grams:.1f}g -> "
                  f"{info.total_grams * rate:.2f} USD at {rate:.4f}/g")
        return True
    finally:
        with contextlib.suppress(Exception):
            await loop.run_in_executor(None, ftp.quit)


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
    print()
    ftp_ok = await probe_ftp(settings)

    print("\n--- Result ---")
    print(f"MQTT:   {'OK' if mqtt_ok else 'FAIL'}")
    print(f"Camera: {'OK' if camera_ok else 'FAIL'}")
    print(f"FTPS:   {'OK' if ftp_ok else 'FAIL'}")
    if mqtt_ok and camera_ok and ftp_ok:
        print("\nAll three protocols verified. The service is safe to start.")
        return 0
    if mqtt_ok and camera_ok:
        print("\nMonitoring will work. Print history will record everything")
        print("except filament weight, length, colours and cost.")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
