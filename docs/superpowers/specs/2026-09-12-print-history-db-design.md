# Print History Database and Filament Accounting - Design

Date: 2026-09-12
Status: Approved
Builds on: `2026-09-12-bambu-watch-design.md` (V1)

## 1. Purpose

Record every print in a queryable local database: name, dates, outcome,
filament weight and length, colours used, and cost. For a print that stopped
partway, record what it actually consumed rather than what it was going to
consume.

The weight and colour figures come from the sliced file on the printer's SD
card, fetched over FTPS. Cost is derived from the operator's spool price.

## 2. Hard constraint: the printer is read-only

The operator's requirement is that nothing this feature does may affect the
printer or a running print. This section is the controlling constraint and
overrides any other goal in this document, including accuracy.

### 2.1 Current state

V1 already complies. The only message the service ever publishes to the
printer is `pushall`, which requests a status snapshot. The camera connection
is a read-only TCP read. Nothing can pause, stop, resume, or send gcode.

### 2.2 What this feature introduces

FTPS is the first new access path. The risk is not accidental writes; it is
SD card I/O contention, because a P1S streams gcode from the same card that
FTPS serves.

Bambu Studio and OrcaSlicer both browse this channel during prints, which is
reasonable evidence that reads are tolerated. That is not proof that a
sustained multi-megabyte read never causes a stutter, and no such proof is
available. The design therefore assumes the risk is real and removes the
opportunity rather than arguing it away.

### 2.3 Enforcement

1. **Write verbs do not exist.** `BambuFtpClient` implements directory
   listing and binary retrieval only. `STOR`, `APPE`, `DELE`, `MKD`, `RMD`,
   `RNFR`, `RNTO`, and `SITE` are never wrapped. This is enforced by a test
   that scans the source tree, not by convention.
2. **No reads during `RUNNING`.** A fetch is attempted only while the printer
   reports `PREPARE`, or after the print has ended (`FINISH`, `FAILED`,
   `IDLE`). If a fetch is in flight when the state leaves `PREPARE`, it is
   cancelled.
3. **One fetch per print.** The result is cached for the session. There is no
   polling and no retry loop.
4. **Bounded.** Hard connection and transfer timeouts, and a maximum transfer
   size. Exceeding either aborts the fetch.
5. **Always optional.** Every failure is non-fatal and leaves the monitoring
   pipeline untouched, consistent with how the camera and vision layers
   already behave. `ENABLE_SLICE_FETCH=false` disables the feature entirely.

### 2.4 The constraint is tested

`tests/test_printer_readonly.py` asserts, by scanning `app/`:

- no FTP write verb appears in any source file
- the only MQTT publish payload constructed anywhere contains `pushall`
- no gcode or printer-control command string (`pause`, `resume`, `stop`,
  `gcode_line`) is constructed

This is a regression test for the operator's requirement, in the same spirit
as the state-machine regression tests in V1. A future change that adds printer
control fails the suite rather than shipping quietly.

## 3. Decisions

### 3.1 SQLite

One file at `data/bambu_watch.db`, on the volume already mounted into the
container. `sqlite3` is in the standard library, so no new dependency and no
server process on the Pi.

`journal_mode=WAL` so the database can be queried while the service runs.
`synchronous=FULL`, because writes happen twice per print and power loss on a
Pi is plausible; durability matters more than speed at this volume.

Schema version tracked with `PRAGMA user_version`.

### 3.2 Scope: print summaries only

SQLite holds one row per print plus its filaments. Per-check vision results
stay in `detections.jsonl`. That file already works, append-only JSONL suits
a streaming log, and it stays greppable without a database client. The two
are linked by `prints.session_dir`.

### 3.3 Rows are written at start, not only at end

The print row is inserted when monitoring begins, with
`outcome = 'running'`, and updated when the session closes. If the service is
killed or the Pi loses power mid-print, the print still has a record. A row
left at `'running'` on startup is reconciled to `'unknown'`.

### 3.4 AMS, multi-colour

The operator runs an AMS and prints multi-colour, so `print_filaments` is a
real child table rather than a formality, and the AMS block from MQTT is
parsed for live slot colours alongside the per-filament figures from the
sliced file.

## 4. Data model

```sql
PRAGMA user_version = 1;

CREATE TABLE prints (
    id                  TEXT PRIMARY KEY,   -- monitoring session uuid
    file_name           TEXT,               -- subtask_name
    started_at          TEXT NOT NULL,      -- ISO8601 UTC
    ended_at            TEXT,
    outcome             TEXT NOT NULL,      -- running|completed|stopped|failed|unknown
    final_state         TEXT,               -- raw gcode_state at close
    duration_seconds    INTEGER,
    progress_at_end     INTEGER,
    layer_at_end        INTEGER,
    total_layers        INTEGER,

    -- planned, from the sliced file
    planned_grams       REAL,
    planned_meters      REAL,
    planned_cost        REAL,

    -- actually consumed
    consumed_grams      REAL,
    consumed_cost       REAL,
    consumption_method  TEXT,               -- layer_fraction|gcode_exact|complete|unknown

    -- provenance
    slice_info_source   TEXT,               -- ftps|unavailable|disabled
    cost_per_gram       REAL,               -- rate in force for this print

    -- monitoring
    checks              INTEGER NOT NULL DEFAULT 0,
    alerts              INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    vision_model        TEXT,
    session_dir         TEXT                -- path to frames and detections.jsonl
);

CREATE INDEX idx_prints_started ON prints(started_at);
CREATE INDEX idx_prints_outcome ON prints(outcome);

CREATE TABLE print_filaments (
    print_id        TEXT NOT NULL REFERENCES prints(id) ON DELETE CASCADE,
    slot            INTEGER NOT NULL,   -- AMS tray index, or -1 for external spool
    filament_type   TEXT,               -- PLA, PETG, ABS
    color           TEXT,               -- #RRGGBB as reported
    used_grams      REAL,
    used_meters     REAL,
    PRIMARY KEY (print_id, slot)
);
```

`slot` is the filament identifier from `slice_info.config`, which is unique
within a print and corresponds to the AMS tray index. An external spool
records `-1`. If the sliced file reports filaments with no identifier, they
are numbered by their order of appearance so the primary key stays unique; a
genuine collision means the file is malformed and the whole slice info is
discarded rather than partially written.

`cost_per_gram` is stored per print rather than only in configuration, so
history stays accurate after a change of supplier or spool price.

API cost is deliberately not a column. Model pricing changes, so the row
stores token counts and cost is computed at query time against whatever rate
is current.

## 5. Cost model

The operator's spool is 1kg at 13 USD.

```
cost_per_gram = spool_cost / spool_weight_g   # 13.0 / 1000.0 = 0.013
```

Both are settings (`SPOOL_COST`, `SPOOL_WEIGHT_G`) rather than a hardcoded
per-gram figure, so a different spool size or price needs no code change. A
340g print reads as 4.42 USD.

Cost is uniform per gram across filaments. Per-filament pricing is out of
scope; if the operator later runs materials at different prices, the
`print_filaments` table already carries the split needed to add it.

## 6. Consumption accuracy, and its limits

This section exists because the obvious implementation is quietly wrong and
the database should not present an estimate as a measurement.

For a completed print, consumed equals planned, and
`consumption_method = 'complete'`.

For a print that stopped partway, V1 uses:

```
consumed_grams = planned_grams * (layer_at_end / total_layers)
consumption_method = 'layer_fraction'
```

**This is an estimate, and it is biased by geometry.** Layer count measures
height, not volume. A part with a wide base and a narrow tower will have
consumed far more than its layer fraction suggests; an hourglass errs the
other way. Expect roughly 10-20% error on typical parts and worse on parts
whose cross-section varies sharply with height.

The estimate is labelled as such in the `consumption_method` column, in the
Discord alert wording, and in the README. No query should present a
`layer_fraction` figure as exact.

Exact consumption is deferred, not abandoned. The gcode contains layer
markers and extruder-axis values, so a cumulative grams-per-layer table can
be built once and indexed by `layer_at_end` for a figure accurate to a
fraction of a gram. It is out of scope for this round because the gcode is the
largest file on the card and fetching it is exactly the sustained read section
2 is designed to avoid. Section 10 lists what the probe must establish before
that work is planned.

## 7. Components

| Path | Responsibility |
|---|---|
| `app/bambu/ftp_client.py` | Read-only FTPS client; fetch and parse slice info |
| `app/storage/database.py` | SQLite connection, schema, migrations |
| `app/storage/prints.py` | `PrintRecord` insert and update |
| `app/bambu/ams.py` | Parse the MQTT `ams` block into slot colours |
| `app/detection/monitor.py` | Extended: PREPARE handling, fetch lifecycle, db writes |
| `app/notifications/discord.py` | Extended: material and cost lines when known |
| `scripts/probe_printer.py` | Extended: FTPS probe, section 10 questions |

### 7.1 FTPS client

Bambu printers serve FTPS on port 990 with **implicit** TLS. Python's
`ftplib.FTP_TLS` performs explicit TLS by default, so a small subclass is
required that wraps the socket before reading the greeting. The certificate is
self-signed; verification is disabled, as with MQTT and the camera.

`ftplib` is blocking, so calls run in a thread executor to keep the event loop
free.

Interface:

```python
@dataclass
class FilamentUsage:
    slot: int
    filament_type: str | None
    color: str | None
    used_grams: float | None
    used_meters: float | None

@dataclass
class SliceInfo:
    filaments: list[FilamentUsage]
    total_grams: float
    total_meters: float

class BambuFtpClient:
    def __init__(self, settings: Settings): ...
    async def fetch_slice_info(self, file_name: str) -> SliceInfo | None: ...
```

The sliced file is a 3MF, which is a ZIP. V1 retrieves the whole archive into
memory (bounded by `FTP_MAX_FETCH_BYTES`) and reads
`Metadata/slice_info.config` with `zipfile`. `fetch_slice_info` returns `None`
on any failure, never raises.

### 7.2 Fetch lifecycle in the monitor

```
state PREPARE
   |
   +-- file_name known and not yet attempted for this file
   |      |
   |      +-- launch fetch task
   |             |
   |             +-- completes -> hold SliceInfo on the monitor
   |             +-- state leaves PREPARE -> cancel, mark unattempted
   |             +-- timeout or error -> mark attempted, source unavailable
   |
state RUNNING
   |
   +-- session created; SliceInfo attached; print row inserted as 'running'
   |
   +-- no reads attempted in this state, ever
   |
state FINISH | FAILED | IDLE
   |
   +-- SliceInfo still missing -> attempt now (the print is over, so safe)
   |
   +-- compute outcome and consumption; update print row and filaments
```

The cancel-on-state-change guard is inexpensive insurance in V1, where the
transfer is a few megabytes. It becomes essential if exact gcode parsing is
built later.

If the service starts mid-print, `PREPARE` has already passed and no fetch is
attempted until the print ends. The print still gets a complete row; it simply
has no material figures until close.

### 7.3 Outcome determination

| Observed at close | `outcome` |
|---|---|
| `FINISH` | `completed` |
| `FAILED` | `failed` |
| `IDLE` with `progress_at_end` at or above 99 | `completed` |
| `IDLE` with `progress_at_end` below 99 | `stopped` |
| anything else, or no state | `unknown` |

Whether cancelling from Bambu Handy produces `FAILED` or drops to `IDLE` is
not established. Until the probe answers it, ambiguous closes record as
`unknown` rather than being confidently mislabelled. `unknown` is a real
value, not an error.

## 8. Discord alert

When material figures are known, the alert gains two lines:

```
Progress: 71% - layer 883 / 1240
Material: about 240g of 340g planned (about 3.12 USD, estimated)
```

The word "estimated" is present whenever `consumption_method` is
`layer_fraction`. When slice info is unavailable the lines are omitted
entirely; the alert never shows a placeholder or a zero.

## 9. Configuration additions

| Setting | Default | Purpose |
|---|---|---|
| `SPOOL_COST` | `13.0` | Price paid per spool |
| `SPOOL_WEIGHT_G` | `1000.0` | Spool weight in grams |
| `ENABLE_SLICE_FETCH` | `true` | Master switch for all FTPS access |
| `BAMBU_FTP_PORT` | `990` | FTPS port (implicit TLS) |
| `FTP_TIMEOUT_SECONDS` | `30.0` | Connection and transfer timeout |
| `FTP_MAX_FETCH_BYTES` | `33554432` | 32MB transfer cap |
| `FTP_SEARCH_DIRS` | `/,/cache` | Directories searched for the sliced file |

## 10. Unknowns the probe must establish

These are undocumented and must be answered against real hardware before the
affected behaviour is trusted. The probe script is extended to report each.

1. Does FTPS on 990 accept `bblp` plus the access code, with implicit TLS?
2. Which directory holds the sliced file, and how does its name relate to
   `subtask_name` and `gcode_file`?
3. Does the 3MF contain `Metadata/slice_info.config`, and what are its exact
   element and attribute names for grams and metres?
4. Does that file carry a cost figure, or must cost come from `SPOOL_COST`?
   The design assumes the latter and does not depend on the former.
5. What is a representative 3MF size, and a representative gcode size? This
   decides whether exact parsing is viable in the PREPARE window.
6. Does the FTP server support `REST`? If so, a ranged read can extract
   `slice_info.config` in roughly 20KB instead of the whole archive, and the
   contention question largely disappears.
7. Does cancelling from Handy report `FAILED` or `IDLE`, and are
   `print_error` or `fail_reason` populated?
8. What is the exact shape of the MQTT `ams` block on this printer and
   firmware, including how colours are encoded?

Until 1 through 3 are confirmed, `slice_info_source` records `unavailable`
and the database still captures name, dates, outcome, layers, and monitoring
statistics. **The database is useful before the FTPS path works.** That is
deliberate: the feature degrades rather than blocking.

## 11. Testing

No test may require printer hardware, network access, or API credit.

- **Read-only enforcement** (section 2.4) is the highest-value test here and
  is a source scan, not a mock.
- **Schema and migration**: a fresh database reaches `user_version = 1`;
  opening an existing one is idempotent.
- **Row lifecycle**: inserted as `running`, updated on close; a row left at
  `running` is reconciled to `unknown` on startup.
- **Outcome table**: one case per row of the section 7.3 table, including the
  `IDLE` boundary at 99 percent and the `unknown` fallback.
- **Consumption**: `complete` for a finished print; `layer_fraction` maths for
  a stopped one; `None` planned grams yields `None` consumed rather than zero.
- **Cost**: 13.0 over 1000.0 gives 0.013 per gram; a per-print
  `cost_per_gram` snapshot is unaffected by a later settings change.
- **Slice parsing**: a synthetic 3MF built in the test with `zipfile`, plus a
  malformed archive, a missing member, and an oversized archive.
- **Fetch lifecycle**: fetch cancelled when state leaves `PREPARE`; no fetch
  attempted while `RUNNING`; post-print fetch attempted when slice info is
  missing; `ENABLE_SLICE_FETCH=false` attempts nothing.
- **Multi-colour**: a four-filament slice info produces four
  `print_filaments` rows with distinct slots.
- **Degradation**: with FTPS unavailable, a print still produces a complete
  row with `slice_info_source = 'unavailable'`.

## 12. Out of scope

- Exact gcode-derived consumption (section 6), pending probe answers 5 and 6
- Ranged FTPS reads (probe answer 6)
- Per-filament pricing
- Any write, control, or configuration change on the printer, permanently and
  by design
- A dashboard or reporting UI over the database; SQL is the interface for now
- Backfilling historical prints, as no session data predates this change
