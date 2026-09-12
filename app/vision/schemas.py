"""Structured output schema. The vision model is constrained to this shape,
so no prose parsing is ever required."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FailureType = Literal[
    "none",
    "spaghetti",
    "detached_print",
    "detached_support",
    "layer_shift",
    "midair_printing",
    "warping",
    "nozzle_blob",
    "unknown",
]


class FailureAnalysis(BaseModel):
    status: Literal["healthy", "uncertain", "failure"] = Field(
        description="healthy if the print looks fine, failure only when "
        "continuing would reasonably waste material or ruin quality, "
        "uncertain when the evidence is ambiguous."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    failure_type: FailureType = Field(description="none when status is healthy.")
    severity: Literal["none", "low", "medium", "high"]
    explanation: str = Field(
        description="One or two sentences citing the visual evidence, "
        "including any change between the supplied images."
    )
