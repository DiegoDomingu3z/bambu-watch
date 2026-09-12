"""Compare a cheaper screening model against the recorded production verdicts.

Usage:  .venv/bin/python scripts/eval_screening_model.py [model]

This answers the question the design spec deferred: is a cheaper model safe
to put in front of the detector? Run it when you have enough sessions,
including some that actually failed -- false negatives are the risk that
matters, and a dataset of healthy prints cannot measure them.

Sonnet's verdicts already exist in detections.jsonl, so only Haiku costs
money. Windows are reconstructed the way the monitor built them: frames
N-2, N-1, N, oldest first.
"""
import asyncio
import base64
import json
import pathlib
import sys
from collections import Counter

from anthropic import AsyncAnthropic
from anthropic.lib._parse._transform import transform_schema
from pydantic import TypeAdapter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.bambu.models import PrinterState
from app.config import get_settings
from app.vision.analyzer import downscale_jpeg
from app.vision.prompts import SYSTEM_PROMPT, render_context
from app.vision.schemas import FailureAnalysis

SCREEN_MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-haiku-4-5"
ROOT = pathlib.Path(__file__).resolve().parent.parent

try:
    S = get_settings()
except Exception as exc:
    print(f"Could not load settings: {exc}")
    print("Copy .env.example to .env and fill it in.")
    raise SystemExit(2) from None

client = AsyncAnthropic(api_key=S.anthropic_api_key)
SCHEMA = transform_schema(TypeAdapter(FailureAnalysis).json_schema())


def windows():
    for session in sorted((ROOT / "data" / "sessions").glob("*/")):
        det = session / "detections.jsonl"
        if not det.exists():
            continue
        rows = [json.loads(x) for x in det.read_text().splitlines() if x.strip()]
        for row in rows:
            if not row.get("frame") or row.get("status") is None:
                continue
            n = int(pathlib.Path(row["frame"]).stem)
            paths = [session / "frames" / f"{i:04d}.jpg" for i in (n - 2, n - 1, n)]
            if all(p.exists() for p in paths):
                yield session.name, row, paths


async def screen(paths, row):
    state = PrinterState()
    state.apply_report({"gcode_state": "RUNNING"})
    content = []
    for p in paths:
        data = downscale_jpeg(p.read_bytes(), S.frame_upload_width)
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(data).decode()}})
    content.append({"type": "text", "text": render_context(state, 3, S.normal_interval)})

    # Haiku 4.5 rejects output_config.effort and does not take adaptive
    # thinking, so the request shape differs from the Sonnet path.
    resp = await client.messages.parse(
        model=SCREEN_MODEL, max_tokens=1024, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_format=FailureAnalysis,
    )
    return resp.parsed_output, resp.usage


async def main():
    items = list(windows())
    print(f"reconstructable 3-frame windows: {len(items)}\n")
    if not items:
        return

    agree = disagree = failed = 0
    tin = tout = 0
    haiku_status = Counter()
    sonnet_status = Counter()
    rows = []

    for session, row, paths in items:
        try:
            parsed, usage = await screen(paths, row)
        except Exception as exc:
            print(f"  ERROR {type(exc).__name__}: {exc}")
            failed += 1
            continue
        tin += usage.input_tokens
        tout += usage.output_tokens
        if parsed is None:
            failed += 1
            continue

        sonnet_status[row["status"]] += 1
        haiku_status[parsed.status] += 1
        same = parsed.status == row["status"]
        agree += same
        disagree += not same
        rows.append((session[:22], pathlib.Path(row["frame"]).stem,
                     row["status"], row["confidence"], parsed.status,
                     parsed.confidence, parsed.failure_type))

    print(f"{'session':<24}{'frame':<7}{'RECORDED':<20}"
          f"{SCREEN_MODEL.upper():<24}{'agree':<7}{'screen says'}")
    for s, f, ss, sc, hs, hc, ht in rows:
        mark = "yes" if ss == hs else "NO"
        kind = "" if hs == "healthy" else ht
        print(f"{s:<24}{f:<7}{ss + ' ' + format(sc, '.2f'):<20}"
              f"{hs + ' ' + format(hc, '.2f'):<24}{mark:<7}{kind}")

    total = agree + disagree
    print(f"\nagreement on status: {agree}/{total}"
          f" ({100 * agree / total:.0f}%)" if total else "")
    print(f"sonnet: {dict(sonnet_status)}")
    print(f"haiku:  {dict(haiku_status)}")
    print(f"parse failures: {failed}")
    cost = tin * 1.0 / 1e6 + tout * 5.0 / 1e6
    print(f"\nhaiku tokens: {tin:,} in / {tout:,} out")
    print(f"eval spend: ${cost:.4f}")
    if total:
        per = cost / total
        print(f"{SCREEN_MODEL} per check: ${per:.5f} "
              f"-> ${per * 640:.2f} per 8h print at 45s")

    print()
    print("--- what this does and does not prove ---")
    recorded_failures = sum(1 for r in rows if r[2] == "failure")
    if not recorded_failures:
        print("Every window in this dataset was judged healthy by the production")
        print("model, so this run measures FALSE POSITIVES only. It says nothing")
        print("about whether the screening model would MISS a real failure, which")
        print("is the risk that matters. Do not adopt a screening model on the")
        print("strength of this result alone.")
    else:
        print(f"{recorded_failures} recorded failure window(s) present: check")
        print("above whether the screening model caught each one.")
    confs = sorted({round(r[5], 2) for r in rows})
    print()
    print(f"screening model confidence values seen: {confs}")
    if len(confs) <= 3:
        print("That spread is narrow. The confirmation state machine relies on")
        print("confidence being informative, so a near-constant value makes the")
        print("suspicion and confirmation thresholds meaningless.")


asyncio.run(main())
