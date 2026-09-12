"""Print row lifecycle.

Rows are inserted when monitoring starts and updated at close, so a print
survives the service being killed or the Pi losing power. cost_per_gram is
snapshotted onto each row so history stays accurate after a price change.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings
from app.storage.records import (
    OUTCOME_RUNNING,
    OUTCOME_UNKNOWN,
    compute_consumption,
    determine_outcome,
)

logger = logging.getLogger(__name__)


@dataclass
class FilamentRow:
    slot: int
    filament_type: str | None = None
    color: str | None = None
    used_grams: float | None = None
    used_meters: float | None = None


class PrintRepository:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings

    def insert_running(
        self,
        print_id: str,
        file_name: str | None,
        started_at: datetime,
        session_dir: str | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO prints
                (id, file_name, started_at, outcome, cost_per_gram,
                 vision_model, session_dir)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                print_id,
                file_name,
                started_at.isoformat(),
                OUTCOME_RUNNING,
                self.settings.cost_per_gram,
                self.settings.vision_model,
                session_dir,
            ),
        )

    def finalize(
        self,
        print_id: str,
        *,
        final_state: str | None,
        ended_at: datetime,
        progress: int | None,
        layer: int | None,
        total_layers: int | None,
        planned_grams: float | None,
        planned_meters: float | None,
        checks: int,
        alerts: int,
        input_tokens: int,
        output_tokens: int,
    ) -> str:
        row = self.get(print_id)
        rate = row["cost_per_gram"] if row else self.settings.cost_per_gram
        if rate is None:
            rate = self.settings.cost_per_gram

        duration = None
        if row and row["started_at"]:
            try:
                started = datetime.fromisoformat(row["started_at"])
                duration = int((ended_at - started).total_seconds())
            except ValueError:
                duration = None

        outcome = determine_outcome(final_state, progress)
        consumed_grams, method = compute_consumption(
            planned_grams, layer, total_layers, outcome
        )

        planned_cost = None if planned_grams is None else planned_grams * rate
        consumed_cost = None if consumed_grams is None else consumed_grams * rate

        self.conn.execute(
            """
            UPDATE prints SET
                ended_at = ?, outcome = ?, final_state = ?, duration_seconds = ?,
                progress_at_end = ?, layer_at_end = ?, total_layers = ?,
                planned_grams = ?, planned_meters = ?, planned_cost = ?,
                consumed_grams = ?, consumed_cost = ?, consumption_method = ?,
                checks = ?, alerts = ?, input_tokens = ?, output_tokens = ?
            WHERE id = ?
            """,
            (
                ended_at.isoformat(), outcome, final_state, duration,
                progress, layer, total_layers,
                planned_grams, planned_meters, planned_cost,
                consumed_grams, consumed_cost, method,
                checks, alerts, input_tokens, output_tokens,
                print_id,
            ),
        )
        return outcome

    def save_filaments(self, print_id: str, rows: list[FilamentRow]) -> None:
        self.conn.execute("DELETE FROM print_filaments WHERE print_id = ?", (print_id,))
        self.conn.executemany(
            """
            INSERT INTO print_filaments
                (print_id, slot, filament_type, color, used_grams, used_meters)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (print_id, r.slot, r.filament_type, r.color, r.used_grams, r.used_meters)
                for r in rows
            ],
        )

    def set_slice_source(self, print_id: str, source: str) -> None:
        self.conn.execute(
            "UPDATE prints SET slice_info_source = ? WHERE id = ?", (source, print_id)
        )

    def reconcile_stale(self) -> int:
        """A row still marked running at startup belongs to a print the
        service did not see finish. Record that honestly rather than leaving
        it looking live."""
        cursor = self.conn.execute(
            "UPDATE prints SET outcome = ? WHERE outcome = ?",
            (OUTCOME_UNKNOWN, OUTCOME_RUNNING),
        )
        count = cursor.rowcount or 0
        if count:
            logger.info("reconciled %d interrupted print(s) to unknown", count)
        return count

    def get(self, print_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM prints WHERE id = ?", (print_id,)
        ).fetchone()

    def filaments(self, print_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM print_filaments WHERE print_id = ? ORDER BY slot",
                (print_id,),
            )
        )
