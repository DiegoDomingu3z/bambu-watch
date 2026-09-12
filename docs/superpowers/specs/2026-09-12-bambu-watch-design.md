# Bambu P1S AI Print Monitor (bambu-watch) - Design

Date: 2026-09-12
Status: Approved

## 1. Purpose

A Raspberry Pi service that watches a Bambu Lab P1S while printing, samples
the chamber camera, sends short chronological image sequences to Claude for
failure analysis, requires a second matching detection before alerting, and
posts a Discord webhook with evidence.

V1 is strictly advisory. The service never controls or pauses the printer.
The user verifies the alert and pauses manually through Bambu Handy.

## 2. Relationship to the source plan

The project brief supplied by the user is the base specification. It defines
the architecture, module layout, sampling strategy, prompt, confirmation
state machine, cooldown policy, session storage, Docker deployment, V1 scope,
and the detection policy constants. All of that is adopted as written.

This document records only what the brief did not settle, plus corrections to
defects found while reviewing it. Where this document and the brief disagree,
this document wins.

## 3. Decisions

### 3.1 Vision provider: Claude

The brief's `.env` named `OPENAI_API_KEY`. V1 uses Claude via the official
`anthropic` Python SDK instead.

- Default model: `claude-sonnet-5`
- Structured output: `client.messages.parse(output_format=FailureAnalysis)`,
  which constrains the response to the Pydantic schema. No prose parsing.
- Thinking: `{"type": "adaptive"}`. `budget_tokens` is rejected with a 400 on
  Sonnet 5 and must not be used.
- Effort: `output_config={"effort": "low"}`. Verified against `anthropic`
  1.5.0: `messages.parse` merges a caller-supplied `output_config` with the
  schema it injects, so effort and structured output compose.
- `ParsedMessage.parsed_output` is an `Optional` property. A `None` result, or
  a `stop_reason` of `refusal`, is treated as an inconclusive check and never
  as a failure detection.

### 3.2 Cost control

Each check uploads three frames, and a rolling buffer means each frame is
uploaded on three successive checks. Claude bills images at roughly
`(width * height) / 750` tokens, so the brief's 45-second interval at the
camera's native 1280x720 is more expensive than it appears.

Calculated estimates per 8-hour print (640 checks). These are derived from
the token formula, not measured, which is why usage logging is mandatory:

| Configuration | Cost per check | Per 8h print |
|---|---|---|
| 3x 1280x720, effort high | ~$0.013 | ~$8.10 |
| 3x 640x360, effort low | ~$0.007 | ~$4.50 |
| Haiku screen + Sonnet confirm | ~$0.003 | ~$2.20 |

V1 ships the middle row: frames downscaled to 640x360 before upload, effort
low. Model, effort, capture interval, and downscale width are all
configuration settings, so cost can be tuned without a code change.

Two-tier screening is deliberately not built. Whether a cheaper model detects
spaghetti reliably is an empirical question, and the session dataset exists to
answer it. Building it now would be guessing.

Every analysis call appends its token usage to the session's `detections.jsonl`
so real cost per print is measurable rather than estimated.

### 3.3 Repository and secrets

Private GitHub repository under the user's account. The LAN access code is a
device credential: `.env` is gitignored and populated by the user directly.
`.env.example` carries placeholders only. No credential value is ever written
by tooling or committed.

## 4. Corrections to the source plan

The brief's section 14 `process()` sketch and section 8 state machine contain
four defects. The implementation fixes all of them.

### 4.1 Suspicious state leaks

`if result.confidence < 0.80: return` exits without touching
`pending_confirmation`. A single low-confidence result therefore pins the
detector at the 10-second suspicious interval for the rest of the print,
costing roughly six API calls per minute indefinitely.

Fix: an inconclusive result below the suspicion threshold counts against a
bounded suspicion budget and falls through to state cleanup.

### 4.2 Disagreeing failure types deadlock

When the confirming check reports a different `failure_type`, the sketch's
equality test fails and the function falls through without updating state.
`pending_confirmation` keeps holding the stale first result forever.

Fix: a differing high-confidence failure replaces the pending candidate and
restarts confirmation, matching the brief's stated intent in section 8 that
disagreement keeps the system in suspicious mode.

### 4.3 No suspicion timeout

Nothing in the brief ever abandons a suspicion that neither confirms nor
clears.

Fix: `SUSPICION_MAX_CHECKS` (default 6). On exhaustion the detector reverts
to healthy and the normal interval.

### 4.4 Missing MQTT `pushall`

The P1S publishes only incremental reports after a client connects. Without an
explicit request, `PrinterState` can stay mostly empty for minutes and the
service will not notice a print already in progress.

Fix: on every successful connect and subscribe, publish
`{"pushing": {"sequence_id": "<n>", "command": "pushall"}}` to
`device/<serial>/request` to force a full state snapshot.

## 5. Additions

### 5.1 Filename field

`gcode_file` reports as `Metadata/plate_1.gcode` and is useless in an alert.
`subtask_name` carries the human-readable print name and is preferred, with
`gcode_file` as fallback.

### 5.2 Network resilience

Both network edges reconnect with bounded exponential backoff, and neither can
terminate the monitor loop:

- MQTT: automatic reconnect, re-subscribe, and re-issue `pushall`.
- Camera: a failed capture is recorded as a skipped check. The monitor
  continues at the current interval rather than raising.

### 5.3 Camera protocol risk

The P1S chamber camera on TCP 6000 is undocumented. The implementation targets
TLS with certificate verification disabled, an 80-byte authentication payload
carrying `bblp` and the access code in fixed-width fields, then
length-prefixed JPEG frames.

This byte layout is derived from open-source implementations rather than
vendor documentation and may be wrong. It is isolated behind the `Camera`
protocol so a correction touches one module. A probe script
(`scripts/probe_printer.py`) validates both the camera handshake and the MQTT
report shape against real hardware, writing a real JPEG and a real report
payload to disk as fixtures.

## 6. Detection policy

Configuration defaults, unchanged from the brief's section 19 except where
noted:

| Setting | Default |
|---|---|
| Normal check interval | 45s |
| Suspicious check interval | 10s |
| Suspicion threshold | 0.80 |
| Confirmation threshold | 0.85 |
| Total matching detections required | 2 (initial + 1 confirming) |
| Suspicion max checks (new) | 6 |
| Alert cooldown | 15 min |
| Frame history | 3 |
| Vision model | claude-sonnet-5 |
| Vision effort | low |
| Frame upload width | 640 |

## 7. Testing

The detector state machine is pure logic, takes no I/O, and is where the
brief's defects lived. It carries the heaviest test coverage, including a
regression test per defect in section 4.

MQTT report normalization is tested against recorded fixture payloads,
including incremental reports that set only a subset of fields. The vision
analyzer is tested with a stubbed client so no test spends API credit. No test
requires printer hardware or network access.

## 8. Out of scope for V1

Local OpenCV pre-filtering, alternate cameras, the web dashboard, trained
detectors, and any form of automatic printer control. The brief's sections 12
and 18 describe these as later versions and V1 does not anticipate them beyond
keeping the `Camera` protocol substitutable.
