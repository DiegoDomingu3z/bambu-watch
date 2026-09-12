"""Schema v2: money spent on tokens, and a picture of the finished print."""

import sqlite3

import pytest

from app.storage.database import SCHEMA_V1, SCHEMA_VERSION, connect


def columns(conn) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(prints)")}


def test_v2_columns_exist(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert {"api_cost", "api_input_rate", "api_output_rate",
            "final_frame_path", "final_frame_jpeg"} <= columns(conn)


def test_fresh_database_is_at_current_version(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_v1_database_with_rows_migrates_without_loss(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(SCHEMA_V1)
    raw.execute("PRAGMA user_version = 1")
    raw.execute("INSERT INTO prints (id, file_name, started_at, outcome, checks,"
                " consumed_cost) VALUES ('legacy', 'Old', '2026-09-01T00:00:00',"
                " 'completed', 42, 3.50)")
    raw.commit()
    raw.close()

    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = conn.execute("SELECT * FROM prints WHERE id='legacy'").fetchone()
    assert row["file_name"] == "Old", "existing data must survive"
    assert row["checks"] == 42
    assert row["api_cost"] is None, "a pre-v2 row has no recorded api cost"


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    connect(path).close()
    connect(path).close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_blob_round_trips(tmp_path):
    conn = connect(tmp_path / "x.db")
    blob = bytes(range(256)) * 8
    conn.execute("INSERT INTO prints (id, started_at, outcome, final_frame_jpeg)"
                 " VALUES ('a', 'now', 'completed', ?)", (blob,))
    got = conn.execute("SELECT final_frame_jpeg FROM prints WHERE id='a'").fetchone()[0]
    assert got == blob


def test_summary_view_totals_filament_and_tokens(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute("INSERT INTO prints (id, started_at, outcome, consumed_cost,"
                 " api_cost) VALUES ('a', 'now', 'completed', 4.42, 1.17)")
    row = conn.execute("SELECT * FROM print_summary WHERE id='a'").fetchone()
    assert row["filament_cost"] == pytest.approx(4.42)
    assert row["api_cost"] == pytest.approx(1.17)
    assert row["total_cost"] == pytest.approx(5.59)
    assert row["has_image"] == 0


def test_summary_view_tolerates_missing_costs(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute("INSERT INTO prints (id, started_at, outcome) "
                 "VALUES ('a', 'now', 'unknown')")
    row = conn.execute("SELECT total_cost, has_image FROM print_summary").fetchone()
    assert row["total_cost"] == 0, "unknown costs must not break the total"
    assert row["has_image"] == 0


def test_summary_view_reports_image_presence(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute("INSERT INTO prints (id, started_at, outcome, final_frame_jpeg)"
                 " VALUES ('a', 'now', 'completed', ?)", (b"\xff\xd8x\xff\xd9",))
    assert conn.execute("SELECT has_image FROM print_summary").fetchone()[0] == 1


def test_summary_view_excludes_the_blob(tmp_path):
    """A dashboard selecting from the view must not drag every image with it."""
    conn = connect(tmp_path / "x.db")
    names = {r[1] for r in conn.execute("PRAGMA table_info(print_summary)")}
    assert "final_frame_jpeg" not in names
    assert "has_image" in names
