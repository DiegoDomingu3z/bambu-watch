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

SCHEMA_VERSION = 1

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
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    if version == 0:
        conn.executescript(SCHEMA_V1)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info("initialised print history schema v%d", SCHEMA_VERSION)
