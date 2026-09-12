# bambu-watch

An advisory AI print-failure monitor for the Bambu Lab P1S, built to run on a
Raspberry Pi.

It watches the printer over your LAN, samples the chamber camera while a print
is running, asks Claude whether the print is failing, and posts a Discord alert
with the image when two consecutive checks agree.

**It never pauses or controls the printer.** The AI detects and provides
evidence; you decide and pause manually through Bambu Handy. That boundary is
deliberate and is not a missing feature.

```
P1S prints
   |
Raspberry Pi monitors
   |
Vision model detects possible failure
   |
Second check confirms
   |
Discord alert + image
   |
You verify
   |
You pause through Bambu Handy
```

## How it works

```
Bambu P1S ---- MQTT/TLS :8883 ----> PrinterState (normalized)
          ---- JPEG/TLS :6000 ----> SnapshotBuffer (3 frames)
                                          |
                                    VisionAnalyzer ----> Claude API
                                          |
                                    FailureDetector
                                     (needs 2 matches)
                                          |
                                    DiscordNotifier ----> Discord
```

Only outbound connections are made: to the printer on the LAN, to the Claude
API, and to Discord. Nothing listens for inbound traffic.

## Requirements

- A Bambu Lab P1S on the same network, with LAN mode reachable
- The printer's IP, serial number, and LAN access code
- An Anthropic API key
- A Discord webhook URL
- Docker, or Python 3.12 with `uv`

## Setup

```bash
git clone <your-repo-url> && cd bambu-watch
cp .env.example .env
```

Fill in `.env` yourself. On the printer, the access code is under
**Settings > WLAN**; the serial is under **Settings > Device**. `.env` is
gitignored and must never be committed.

For local development:

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
```

## Verify the hardware first

Two of the printer's protocols are undocumented: the MQTT report shape and the
camera stream on port 6000. Before trusting the service, confirm both against
your actual printer.

```bash
.venv/bin/python scripts/probe_printer.py
```

It writes `fixtures/live/report.json` and `fixtures/live/frame.jpg` and prints
the captured frame's resolution and token cost. Run it while a print is
actually running so the report contains layer and progress fields.

If the camera step fails, the handshake in
[app/bambu/camera.py](app/bambu/camera.py) needs correcting -- specifically
`build_auth_packet` and `read_frame`. Nothing else in the pipeline depends on
those byte offsets, so a fix is contained to that one module.

## Run

```bash
docker compose up -d
```

`restart: unless-stopped` brings the service back after a Pi reboot, and the
MQTT client reconnects on its own when the network or the printer drops.

```bash
docker compose logs -f
```

## Configuration

Every value below is an environment variable in `.env`. Defaults are what ship.

| Variable | Default | What it does |
|---|---|---|
| `BAMBU_HOST` | required | Printer IP on your LAN |
| `BAMBU_SERIAL` | required | Printer serial; forms the MQTT topic |
| `BAMBU_ACCESS_CODE` | required | LAN access code, from the printer screen |
| `BAMBU_MQTT_PORT` | `8883` | MQTT/TLS port |
| `BAMBU_CAMERA_PORT` | `6000` | Chamber camera port |
| `ANTHROPIC_API_KEY` | required | Claude API key |
| `VISION_MODEL` | `claude-sonnet-5` | Model used for classification |
| `VISION_EFFORT` | `low` | Reasoning depth; raises accuracy and cost |
| `VISION_MAX_TOKENS` | `2048` | Response cap |
| `FRAME_UPLOAD_WIDTH` | `640` | Frames downscaled to this width before upload |
| `DISCORD_WEBHOOK_URL` | required | Where alerts go |
| `NORMAL_INTERVAL` | `45` | Seconds between checks while healthy |
| `SUSPICIOUS_INTERVAL` | `10` | Seconds between checks while confirming |
| `SUSPICION_THRESHOLD` | `0.80` | Confidence that raises suspicion |
| `CONFIRMATION_THRESHOLD` | `0.85` | Confidence required to confirm and alert |
| `SUSPICION_MAX_CHECKS` | `6` | Checks before an unresolved suspicion is dropped |
| `ALERT_COOLDOWN_MINUTES` | `15` | Repeat suppression per failure type |
| `FRAME_HISTORY` | `3` | Frames sent per check |
| `DATA_DIR` | `data` | Where sessions are written |
| `SAVE_FRAMES` | `true` | Persist frames for later evaluation |
| `MAX_SESSION_FRAMES` | `2000` | Cap so one print cannot fill the disk |

## API cost

Claude bills images at roughly `(width * height) / 750` tokens. Each check
sends three frames, and because the buffer rolls, each frame is uploaded on
three successive checks.

At the shipped defaults -- 640x360 frames, `effort: low`, `claude-sonnet-5` --
one check costs about 1,550 input tokens, which is roughly **$4.50 for an
8-hour print**. Native 1280x720 at `effort: high` is closer to **$8.10**, and
`claude-opus-5` roughly triples whichever you pick.

These are calculated from the token formula, not measured. Every check appends
its real usage to `detections.jsonl`, so after a print you can total the
actual cost:

```bash
jq -s 'map(.input_tokens) | add' data/sessions/*/detections.jsonl
```

Four levers reduce spend, in rough order of effect:

1. `NORMAL_INTERVAL` -- doubling it halves the cost
2. `FRAME_UPLOAD_WIDTH` -- cost scales with area, so 480 is ~44% of 640
3. `VISION_EFFORT` -- `low` already; this only matters if you raised it
4. `VISION_MODEL` -- Sonnet is the default for this reason

## Session data

```
data/sessions/2026-09-12_143022_Mask_Final_v17/
  metadata.json      print name, totals, token usage, final state
  detections.jsonl   one row per check, with confidence and usage
  frames/
    0001.jpg
    0002.jpg
```

This accumulates a dataset of real prints and real failures, which is what
makes the detector's accuracy measurable rather than a guess. To count alerts
that you judged wrong, mark them and compare:

```bash
# every check that triggered an alert
jq -c 'select(.alert == true)' data/sessions/*/detections.jsonl

# confidence distribution of everything the model called a failure
jq -r 'select(.status == "failure") | .confidence' \
  data/sessions/*/detections.jsonl | sort -n
```

Once you have enough prints, that data is what tells you whether to adjust
`CONFIRMATION_THRESHOLD`, and whether automatic pausing would ever be safe.

## Troubleshooting

**MQTT connect refused.** The access code is wrong, or LAN mode is off. The
code rotates when you re-provision the printer's network. Verify with
`scripts/probe_printer.py`.

**Connected, but `PrinterState` stays empty.** The printer sends only
incremental reports; the client forces a full snapshot with a `pushall` request
on connect. If state is still empty, the serial in `.env` is wrong, so the
client is subscribed to a topic nothing publishes to.

**Camera handshake fails.** Expected -- the port-6000 protocol is
reverse-engineered. Correct `build_auth_packet` / `read_frame` in
[app/bambu/camera.py](app/bambu/camera.py). Everything else is isolated from
those byte offsets.

**No alerts on an obviously failed print.** Two checks at or above
`CONFIRMATION_THRESHOLD` with the same failure type are required. Check
`detections.jsonl` for what the model actually reported; if confidence is
consistently just below the bar, lower it.

**Too many false alerts.** Raise `CONFIRMATION_THRESHOLD`, or raise
`VISION_EFFORT`, or move to `claude-opus-5`. Use the recorded sessions to
decide which, rather than guessing.

## Development

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check app tests scripts
```

No test requires printer hardware, network access, or an API key, and no test
spends API credit. The vision client is stubbed.

The heaviest coverage is on
[app/detection/confirmation.py](app/detection/confirmation.py), the
confirmation state machine. It is pure logic with no I/O, it decides whether
you get woken up at 3am, and it carries a named regression test for each defect
found while reviewing the original design.

Design notes and the implementation plan are in
[docs/superpowers](docs/superpowers).

## Scope

V1 is only what is described above. Deliberately absent: local OpenCV
pre-filtering, alternate camera sources, a web dashboard, trained detectors,
and any form of automatic printer control.
