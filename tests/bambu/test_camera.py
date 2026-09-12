import asyncio

import pytest

from app.bambu.camera import build_auth_packet, read_frame

JPEG = b"\xff\xd8" + b"body" + b"\xff\xd9"


def test_auth_packet_is_80_bytes():
    assert len(build_auth_packet("bblp", "abcd1234")) == 80


def test_auth_packet_header_and_fixed_width_fields():
    pkt = build_auth_packet("bblp", "abcd1234")
    assert pkt[:4] == (0x40).to_bytes(4, "little")
    assert pkt[4:8] == (0x3000).to_bytes(4, "little")
    assert pkt[8:12] == b"\x00\x00\x00\x00"
    assert pkt[12:16] == b"\x00\x00\x00\x00"
    assert pkt[16:48].rstrip(b"\x00") == b"bblp"
    assert pkt[48:80].rstrip(b"\x00") == b"abcd1234"


def test_auth_packet_rejects_oversized_credentials():
    with pytest.raises(ValueError):
        build_auth_packet("bblp", "x" * 33)


class FakeReader:
    def __init__(self, data: bytes):
        self._data = data
        self.pos = 0

    async def readexactly(self, n: int) -> bytes:
        if self.pos + n > len(self._data):
            raise asyncio.IncompleteReadError(self._data[self.pos:], n)
        chunk = self._data[self.pos:self.pos + n]
        self.pos += n
        return chunk


def framed(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "little") + b"\x00" * 12 + payload


async def test_read_frame_returns_jpeg_payload():
    assert await read_frame(FakeReader(framed(JPEG))) == JPEG


async def test_read_frame_rejects_implausible_length():
    bad = (500_000_000).to_bytes(4, "little") + b"\x00" * 12
    with pytest.raises(ValueError, match="implausible"):
        await read_frame(FakeReader(bad))


async def test_read_frame_rejects_zero_length():
    with pytest.raises(ValueError, match="implausible"):
        await read_frame(FakeReader(b"\x00" * 16))


async def test_read_frame_rejects_payload_without_jpeg_magic():
    with pytest.raises(ValueError, match="JPEG"):
        await read_frame(FakeReader(framed(b"\x00\x01not a jpeg")))


async def test_read_frame_propagates_truncated_stream():
    with pytest.raises(asyncio.IncompleteReadError):
        await read_frame(FakeReader(framed(JPEG)[:10]))
