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

## Notifications

Three kinds, all over the same webhook:

| Event | API cost | Attachment |
|---|---|---|
| Print started | none | none |
| Possible failure detected | one vision call | the triggering frame |
| Print finished, stopped or failed | none | the last frame captured |

Start and finish messages cost nothing in API spend; they are webhook POSTs
built from data the service already has. Turn either off with
`NOTIFY_ON_START=false` / `NOTIFY_ON_FINISH=false`.

```
PRINT STARTED

Print: 01_Platform_AMS
Layers: 300
Material: 340g planned, 4.42 USD
Colours: PLA #FF0000, PLA #00FF00

AI monitoring active. This service never pauses the printer.
```

```
PRINT STOPPED

Print: 02_Mini_Modular_Display_P1S
Outcome: stopped
Duration: 52m
Layer: 118 / 300
Material wasted: 134g (1.74 USD, estimated)
Checks: 35  Alerts: 1
Monitoring cost: 0.16 USD
```

A completed print says "used" rather than "wasted" and omits "estimated",
since a finished print consumed its whole estimate. Material lines are absent
entirely when the sliced file was never fetched - never shown as zero.

`Monitoring cost` is computed from recorded tokens at
`VISION_INPUT_COST_PER_MTOK` / `VISION_OUTPUT_COST_PER_MTOK`, which default to
claude-sonnet-5 rates. **Update them if you change `VISION_MODEL`**, or the
figure will be wrong.

## Print history

Every print is recorded in a SQLite database at `data/bambu_watch.db`: name,
dates, outcome, filament weight and length, colours used, and cost. A print
that stopped partway records what it actually consumed.

`sqlite3` is in the standard library, so there is no extra dependency and no
server process on the Pi. Per-check vision results stay in
`detections.jsonl`; the two are linked by `prints.session_dir`.

### The printer stays read-only

Weight and colour figures come from the sliced 3MF on the printer's SD card,
fetched over FTPS. That is the only new access path this feature adds, and
the risk it carries is **SD card I/O contention** rather than accidental
writes, because the printer streams gcode from the same card.

Three mechanisms, not one promise:

1. **Write verbs do not exist.** The FTPS client's entire vocabulary is
   `RETR`. `STOR`, `DELE`, `MKD`, `RMD`, `RNFR` and friends are never
   wrapped.
2. **No read is ever attempted while the printer reports `RUNNING`.** A fetch
   happens during `PREPARE`, when nothing is extruding, or after the print
   ends. A transfer still in flight when the printer leaves `PREPARE` is
   cancelled.
3. **It is enforced by a test.** `tests/test_printer_readonly.py` scans the
   source and fails if any FTP write command appears, if the client learns a
   command other than `RETR`, if a printer-control string is constructed, or
   if anything other than `pushall` is ever published over MQTT.

Set `ENABLE_SLICE_FETCH=false` to disable FTPS entirely. The database still
records name, dates, outcome, layers and monitoring statistics; only the
material figures go missing.

### Consumption for a stopped print is an estimate

For a completed print, consumed equals planned and `consumption_method` is
`complete`.

For a print that stopped partway, consumed grams are
`planned_grams x (layer_at_end / total_layers)` and the method is
`layer_fraction`.

**That is an estimate, and it is biased by geometry.** Layer count measures
height, not volume. A part with a wide base and a narrow tower consumed far
more than its layer fraction suggests; an hourglass errs the other way.
Expect roughly 10-20% error on typical parts and worse where cross-section
changes sharply with height.

Never present a `layer_fraction` figure as exact. Discord alerts say
"estimated" whenever the number came from that path. Exact consumption is
possible by parsing per-layer extrusion out of the gcode; it is deliberately
not built, because the gcode is the largest file on the card and fetching it
is the sustained read the design above avoids.

### Schema

```
prints             one row per print
  id, file_name, started_at, ended_at
  outcome            running | completed | stopped | failed | unknown
  final_state, duration_seconds
  progress_at_end, layer_at_end, total_layers
  planned_grams, planned_meters, planned_cost
  consumed_grams, consumed_cost, consumption_method
  slice_info_source  ftps | unavailable | disabled
  cost_per_gram      the rate in force for this print
  checks, alerts, input_tokens, output_tokens, vision_model, session_dir

print_filaments    one row per filament slot
  print_id, slot     AMS tray index, or -1 for the external spool
  filament_type, color, used_grams, used_meters
```

Plus, on every print: `api_cost` with the `api_input_rate` and
`api_output_rate` that produced it, and the finishing image as
`final_frame_path` (full resolution, on disk) and `final_frame_jpeg` (a
downscaled copy in the database).

A row is inserted when monitoring starts, with `outcome = 'running'`, and
updated at close. If the Pi loses power mid-print the print still has a
record; a row still marked `running` at startup is reconciled to `unknown`.

`cost_per_gram` is stored per row, so changing `SPOOL_COST` never rewrites
what past prints cost.

### For a dashboard

`print_summary` is a view built for exactly this. It totals filament and
token cost, reports whether an image exists, and **excludes the image blob**
so selecting from it stays cheap:

```sql
SELECT file_name, outcome, duration_seconds,
       filament_cost, api_cost, total_cost, has_image
FROM print_summary
ORDER BY started_at DESC;
```

Pull one image out when you actually want to render it:

```bash
sqlite3 data/bambu_watch.db \
  "SELECT writefile('/tmp/last.jpg', final_frame_jpeg) FROM prints
   WHERE final_frame_jpeg IS NOT NULL ORDER BY started_at DESC LIMIT 1;"
```

Two things stored per image, deliberately. `final_frame_path` points at the
full-resolution frame on disk. `final_frame_jpeg` is a downscaled copy
(`FINAL_FRAME_WIDTH`, default 640) held in the database, so a dashboard can
render straight from one file with no filesystem join, and still works after
session directories are pruned. At roughly 40KB per print, a thousand prints
is about 40MB. Set `STORE_FINAL_FRAME=false` to keep only the path.

### Cost

```
cost_per_gram = SPOOL_COST / SPOOL_WEIGHT_G
```

Defaults are `13.0 / 1000` = **$0.013 per gram**, so a 340g print is $4.42.
Change either setting for a different spool; no code change needed.

| Variable | Default | Purpose |
|---|---|---|
| `SPOOL_COST` | `13.0` | Price paid per spool |
| `SPOOL_WEIGHT_G` | `1000.0` | Spool weight in grams |
| `ENABLE_SLICE_FETCH` | `true` | Master switch for all FTPS access |
| `BAMBU_FTP_PORT` | `990` | FTPS port, implicit TLS |
| `FTP_TIMEOUT_SECONDS` | `30.0` | Connection and transfer timeout |
| `FTP_MAX_FETCH_BYTES` | `33554432` | 32MB transfer cap |
| `FTP_SEARCH_DIRS` | `/,/cache` | Directories searched for the sliced file |

### Queries

```bash
sqlite3 data/bambu_watch.db
```

```sql
-- spend and material this month
SELECT COUNT(*) AS prints,
       ROUND(SUM(consumed_grams)) AS grams,
       ROUND(SUM(consumed_cost), 2) AS usd
FROM prints
WHERE started_at >= date('now', 'start of month');

-- what failures actually cost, worst first
SELECT file_name, outcome, consumption_method,
       ROUND(consumed_grams) AS grams, ROUND(consumed_cost, 2) AS usd
FROM prints
WHERE outcome IN ('stopped', 'failed')
ORDER BY consumed_cost DESC;

-- filament used by colour
SELECT color, filament_type, ROUND(SUM(used_grams)) AS grams
FROM print_filaments
GROUP BY color, filament_type
ORDER BY grams DESC;

-- did the monitoring earn its keep? what each print cost to watch
-- against what its filament was worth
SELECT file_name, outcome, alerts,
       ROUND(filament_cost, 2) AS filament_usd,
       ROUND(api_cost, 2)      AS watching_usd,
       ROUND(total_cost, 2)    AS total_usd
FROM print_summary
ORDER BY started_at DESC;

-- total spent on watching, all time
SELECT ROUND(SUM(api_cost), 2) AS usd_on_tokens,
       ROUND(SUM(filament_cost), 2) AS usd_on_filament
FROM print_summary;
```

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

**No weight or cost in the database.** `slice_info_source` says why.
`disabled` means `ENABLE_SLICE_FETCH=false`. `unavailable` means the archive
was not found or not parseable: run `scripts/probe_printer.py` mid-print,
which reports which directory holds the sliced file and whether
`Metadata/slice_info.config` parses. Note that if the service started while a
print was already running, `PREPARE` had passed, so figures only arrive when
that print ends.

**A print is stuck showing `running`.** The service did not see it finish,
usually because it was killed or the Pi lost power. The next startup
reconciles those rows to `unknown`.

**Cancelled prints record as `unknown` rather than `stopped`.** Whether
cancelling from Handy reports `FAILED` or drops to `IDLE` is undocumented, so
an ambiguous close is recorded honestly instead of being guessed at. The probe
script reports which fields your firmware populates.

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
