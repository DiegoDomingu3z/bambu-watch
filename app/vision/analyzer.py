"""Claude vision analyzer.

Frames are downscaled before upload because images are billed at roughly
(width * height) / 750 tokens and a rolling buffer uploads each frame on
three successive checks. Token usage is returned so real cost per print is
measurable rather than estimated.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from PIL import Image

from app.bambu.models import ImageFrame, PrinterState
from app.config import Settings
from app.vision.prompts import SYSTEM_PROMPT, render_context
from app.vision.schemas import FailureAnalysis

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    analysis: FailureAnalysis | None
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str | None = None


def downscale_jpeg(jpeg: bytes, width: int) -> bytes:
    """Shrink to `width` preserving aspect ratio. Returns the input unchanged
    if it is already narrower or cannot be decoded -- an unreadable frame is
    the camera layer's problem, not a reason to fail the check here."""
    try:
        img = Image.open(io.BytesIO(jpeg))
        img.load()
    except Exception:
        logger.warning("frame could not be decoded for downscaling")
        return jpeg

    if img.width <= width:
        return jpeg

    height = max(1, round(img.height * width / img.width))
    resized = img.convert("RGB").resize((width, height), Image.LANCZOS)
    out = io.BytesIO()
    resized.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


class VisionAnalyzer:
    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None):
        self.settings = settings
        self._client = client or AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def analyze(
        self, frames: list[ImageFrame], state: PrinterState, interval: int
    ) -> AnalysisResult:
        content: list[dict] = []
        for frame in frames:
            data = downscale_jpeg(frame.jpeg, self.settings.frame_upload_width)
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(data).decode("ascii"),
                    },
                }
            )
        content.append(
            {"type": "text", "text": render_context(state, len(frames), interval)}
        )

        try:
            response = await self._client.messages.parse(
                model=self.settings.vision_model,
                max_tokens=self.settings.vision_max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_format=FailureAnalysis,
                output_config={"effort": self.settings.vision_effort},
                thinking={"type": "adaptive"},
            )
        except Exception as exc:
            # A failed analysis is a skipped check. The monitor keeps running.
            logger.warning("vision analysis failed: %s", exc)
            return AnalysisResult(analysis=None, model=self.settings.vision_model)

        stop_reason = getattr(response, "stop_reason", None)
        usage = getattr(response, "usage", None)

        analysis = response.parsed_output
        if stop_reason == "refusal":
            # Never let a safety refusal read as a failure detection.
            logger.warning("vision request refused; treating check as inconclusive")
            analysis = None

        return AnalysisResult(
            analysis=analysis,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            model=self.settings.vision_model,
            stop_reason=stop_reason,
        )
