"""Entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from pydantic import ValidationError

from app.bambu.camera import P1SCamera
from app.bambu.ftp_client import BambuFtpClient
from app.bambu.mqtt_client import BambuMqttClient
from app.config import get_settings
from app.detection.monitor import PrintMonitor
from app.notifications.discord import DiscordNotifier
from app.storage.database import connect
from app.storage.prints import PrintRepository
from app.vision.analyzer import VisionAnalyzer

logger = logging.getLogger("bambu_watch")


def build_repository(settings) -> PrintRepository | None:
    """Print history is a convenience. Monitoring is the job, so a database
    that cannot be opened is logged and skipped rather than fatal."""
    try:
        repository = PrintRepository(connect(settings.db_path), settings)
        repository.reconcile_stale()
        return repository
    except Exception as exc:
        logger.error("print history unavailable: %s", exc)
        return None


def build_monitor() -> PrintMonitor:
    settings = get_settings()
    return PrintMonitor(
        settings=settings,
        printer=BambuMqttClient(settings),
        camera=P1SCamera(settings),
        analyzer=VisionAnalyzer(settings),
        notifier=DiscordNotifier(settings),
        repository=build_repository(settings),
        ftp=BambuFtpClient(settings) if settings.enable_slice_fetch else None,
    )


async def run() -> None:
    settings = get_settings()
    monitor = build_monitor()
    logger.info(
        "bambu-watch starting: printer=%s model=%s interval=%ss width=%s",
        settings.bambu_host,
        settings.vision_model,
        settings.normal_interval,
        settings.frame_upload_width,
    )
    logger.info(
        "filament %.4f USD/g (%.2f per %.0fg spool); slice fetch %s",
        settings.cost_per_gram,
        settings.spool_cost,
        settings.spool_weight_g,
        "enabled" if settings.enable_slice_fetch else "disabled",
    )
    logger.info("advisory only: this service never pauses the printer")

    task = asyncio.create_task(monitor.run_forever())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not every platform implements add_signal_handler.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, task.cancel)

    try:
        await task
    except asyncio.CancelledError:
        logger.info("shutting down")


def describe_config_error(exc: ValidationError) -> str:
    """Turn a pydantic validation failure into something a person reading
    `docker compose logs` can act on."""
    missing = sorted(
        str(err["loc"][0]).upper()
        for err in exc.errors()
        if err.get("type") == "missing" and err.get("loc")
    )
    other = [
        f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg')}"
        for err in exc.errors()
        if err.get("type") != "missing"
    ]

    lines = ["Configuration error: bambu-watch cannot start."]
    if missing:
        lines.append("")
        lines.append("These required settings are not set:")
        lines += [f"  {name}" for name in missing]
    for problem in other:
        lines.append(f"  {problem}")
    lines += [
        "",
        "Copy .env.example to .env and fill it in. Under Docker, make sure",
        "the compose file's env_file points at that .env.",
    ]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        get_settings()
    except ValidationError as exc:
        # Exit cleanly rather than crash-looping under restart:
        # unless-stopped, and say what is actually missing.
        print(describe_config_error(exc), file=sys.stderr)
        raise SystemExit(1) from None

    asyncio.run(run())


if __name__ == "__main__":
    main()
