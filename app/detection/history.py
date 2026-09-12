"""Rolling frame buffer. Gives the vision model temporal context instead of
forcing it to judge a print from one image."""

from __future__ import annotations

from collections import deque

from app.bambu.models import ImageFrame


class SnapshotBuffer:
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._frames: deque[ImageFrame] = deque(maxlen=capacity)

    def add(self, frame: ImageFrame) -> None:
        self._frames.append(frame)

    def latest(self, n: int) -> list[ImageFrame]:
        """Oldest first, so the model reads the images as a timeline."""
        if n <= 0:
            return []
        return list(self._frames)[-n:]

    def ready(self) -> bool:
        return len(self._frames) >= self.capacity

    def clear(self) -> None:
        self._frames.clear()

    def __len__(self) -> int:
        return len(self._frames)
