"""SQLite storage for print history.

One file on the data volume. sqlite3 is stdlib, so there is no new dependency
and no server process on the Pi. WAL so the database can be queried while the
service runs; synchronous FULL because writes happen twice per print and power
loss on a Pi is plausible, so durability beats speed at this volume.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS prints (
    id                  TEXT PRIMARY KEY,
    file_name           TEXT,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    outcome             TEXT NOT NULL,
    final_state         TEXT,
    duration_seconds    INTEGER,
    progress_at_end     INTEGER,
    layer_at_end        INTEGER,
    total_layers        INTEGER,
    planned_grams       REAL,
    planned_meters      REAL,
    planned_cost        REAL,
    consumed_grams      REAL,
    consumed_cost       REAL,
    consumption_method  TEXT,
    slice_info_source   TEXT,
    cost_per_gram       REAL,
    checks              INTEGER NOT NULL DEFAULT 0,
    alerts              INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    vision_model        TEXT,
    session_dir         TEXT
);

CREATE INDEX IF NOT EXISTS idx_prints_started ON prints(started_at);
CREATE INDEX IF NOT EXISTS idx_prints_outcome ON prints(outcome);

CREATE TABLE IF NOT EXISTS print_filaments (
    print_id        TEXT NOT NULL REFERENCES prints(id) ON DELETE CASCADE,
    slot            INTEGER NOT NULL,
    filament_type   TEXT,
    color           TEXT,
    used_grams      REAL,
    used_meters     REAL,
    PRIMARY KEY (print_id, slot)
);
"""


SCHEMA_V2 = """
-- Money spent on vision tokens for this print, with the rates that produced
-- it. Rates are snapshotted for the same reason cost_per_gram is: model
-- pricing changes, and history should stay auditable.
ALTER TABLE prints ADD COLUMN api_cost REAL;
ALTER TABLE prints ADD COLUMN api_input_rate REAL;
ALTER TABLE prints ADD COLUMN api_output_rate REAL;

-- A picture of how the print ended. The path points at the full-resolution
-- frame on disk; the blob is a downscaled copy so a dashboard can render
-- from the database alone, with no filesystem join, and still work after
-- session directories are pruned.
ALTER TABLE prints ADD COLUMN final_frame_path TEXT;
ALTER TABLE prints ADD COLUMN final_frame_jpeg BLOB;

-- Convenience for dashboards: total cost of a print, filament plus tokens.
-- Excludes the image blob so selecting from it stays cheap.
CREATE VIEW IF NOT EXISTS print_summary AS
SELECT
    id, file_name, started_at, ended_at, outcome, duration_seconds,
    layer_at_end, total_layers,
    planned_grams, consumed_grams, consumption_method,
    consumed_cost        AS filament_cost,
    api_cost,
    COALESCE(consumed_cost, 0) + COALESCE(api_cost, 0) AS total_cost,
    checks, alerts, vision_model,
    final_frame_path,
    final_frame_jpeg IS NOT NULL AS has_image,
    session_dir
FROM prints;
"""

# Applied in order; each step raises user_version when it succeeds.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to SCHEMA_VERSION.

    Steps run in order and are additive, so a v1 database keeps its rows.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return

    for target, script in MIGRATIONS:
        if version >= target:
            continue
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {target}")
        logger.info("print history schema migrated to v%d", target)
        version = target
