from datetime import UTC, datetime, timedelta

import pytest

from app.bambu.models import ImageFrame
from app.detection.history import SnapshotBuffer


def frame(i: int) -> ImageFrame:
    return ImageFrame(
        timestamp=datetime(2026, 9, 12, tzinfo=UTC) + timedelta(seconds=i),
        jpeg=bytes([i]),
    )


def test_buffer_not_ready_until_capacity():
    b = SnapshotBuffer(capacity=3)
    assert b.ready() is False
    b.add(frame(1))
    b.add(frame(2))
    assert b.ready() is False
    b.add(frame(3))
    assert b.ready() is True


def test_buffer_evicts_oldest():
    b = SnapshotBuffer(capacity=3)
    for i in range(1, 6):
        b.add(frame(i))
    assert len(b) == 3
    assert [f.jpeg[0] for f in b.latest(3)] == [3, 4, 5]


def test_latest_is_chronological_oldest_first():
    b = SnapshotBuffer(capacity=3)
    for i in range(1, 4):
        b.add(frame(i))
    got = b.latest(3)
    assert got[0].timestamp < got[-1].timestamp


def test_latest_caps_at_available():
    b = SnapshotBuffer(capacity=3)
    b.add(frame(1))
    assert len(b.latest(3)) == 1


def test_latest_zero_returns_empty():
    b = SnapshotBuffer(capacity=3)
    b.add(frame(1))
    assert b.latest(0) == []


def test_clear_empties_buffer():
    b = SnapshotBuffer(capacity=3)
    b.add(frame(1))
    b.clear()
    assert len(b) == 0
    assert b.ready() is False


def test_zero_capacity_rejected():
    with pytest.raises(ValueError):
        SnapshotBuffer(capacity=0)
