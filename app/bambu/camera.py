"""P1S chamber camera.

The protocol on TCP 6000 is undocumented. This implementation targets TLS
with verification disabled, an 80-byte auth payload, then length-prefixed
JPEG frames. The byte layout is derived from open-source implementations and
is the most likely thing in this project to be wrong, which is why the whole
protocol lives behind the `Camera` protocol and is validated by
scripts/probe_printer.py against real hardware.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from datetime import UTC, datetime
from typing import Protocol

from app.bambu.models import ImageFrame
from app.config import Settings

logger = logging.getLogger(__name__)

AUTH_PACKET_SIZE = 80
FIELD_SIZE = 32
FRAME_HEADER_SIZE = 16
MAX_FRAME_BYTES = 8 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8"


class Camera(Protocol):
    """Substitutable image source. Nothing downstream knows or cares whether
    frames come from the P1S, a Pi camera, a webcam, or an RTSP stream."""

    async def capture(self) -> ImageFrame: ...


def build_auth_packet(username: str, access_code: str) -> bytes:
    """80 bytes: a 16-byte header then two 32-byte null-padded fields."""
    user = username.encode("ascii")
    code = access_code.encode("ascii")
    if len(user) > FIELD_SIZE or len(code) > FIELD_SIZE:
        raise ValueError(f"username and access code must each be <= {FIELD_SIZE} bytes")

    return b"".join(
        [
            (0x40).to_bytes(4, "little"),
            (0x3000).to_bytes(4, "little"),
            (0x00).to_bytes(4, "little"),
            (0x00).to_bytes(4, "little"),
            user.ljust(FIELD_SIZE, b"\x00"),
            code.ljust(FIELD_SIZE, b"\x00"),
        ]
    )


async def read_frame(reader) -> bytes:
    """Read one length-prefixed JPEG. Raises ValueError if the stream does
    not look like the expected framing, so a desynchronized connection is
    torn down and retried rather than yielding garbage to the model."""
    header = await reader.readexactly(FRAME_HEADER_SIZE)
    length = int.from_bytes(header[:4], "little")
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise ValueError(f"implausible frame length {length}")

    payload = await reader.readexactly(length)
    if not payload.startswith(JPEG_MAGIC):
        raise ValueError("frame payload is not JPEG")
    return payload


class P1SCamera:
    """Opens a connection per capture. The camera stream is continuous and we
    only want one frame every 10-45 seconds, so holding it open would burn
    LAN bandwidth and printer CPU for frames that are discarded."""

    def __init__(self, settings: Settings, timeout: float = 15.0):
        self.settings = settings
        self.timeout = timeout

    async def capture(self) -> ImageFrame:
        jpeg = await asyncio.wait_for(self._capture_once(), timeout=self.timeout)
        return ImageFrame(timestamp=datetime.now(UTC), jpeg=jpeg)

    async def _capture_once(self) -> bytes:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        reader, writer = await asyncio.open_connection(
            self.settings.bambu_host, self.settings.bambu_camera_port, ssl=context
        )
        try:
            writer.write(build_auth_packet("bblp", self.settings.bambu_access_code))
            await writer.drain()
            return await read_frame(reader)
        finally:
            writer.close()
            # Best effort: the frame is already read, so a close error is
            # not worth propagating.
            with contextlib.suppress(Exception):
                await writer.wait_closed()
