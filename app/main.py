"""Entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from app.bambu.camera import P1SCamera
from app.bambu.mqtt_client import BambuMqttClient
from app.config import get_settings
from app.detection.monitor import PrintMonitor
from app.notifications.discord import DiscordNotifier
from app.vision.analyzer import VisionAnalyzer

logger = logging.getLogger("bambu_watch")


def build_monitor() -> PrintMonitor:
    settings = get_settings()
    return PrintMonitor(
        settings=settings,
        printer=BambuMqttClient(settings),
        camera=P1SCamera(settings),
        analyzer=VisionAnalyzer(settings),
        notifier=DiscordNotifier(settings),
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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
