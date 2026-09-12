import sqlite3

import pytest

from app.storage.database import SCHEMA_VERSION, connect


def test_connect_creates_schema(tmp_path):
    conn = connect(tmp_path / "x.db")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"prints", "print_filaments"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_connect_creates_parent_directory(tmp_path):
    connect(tmp_path / "nested" / "deep" / "x.db").close()
    assert (tmp_path / "nested" / "deep" / "x.db").exists()


def test_reopening_is_idempotent(tmp_path):
    path = tmp_path / "x.db"
    connect(path).close()
    conn = connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_pragmas_are_set(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_rows_are_mappings(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute(
        "INSERT INTO prints (id, started_at, outcome) VALUES ('a', 'now', 'running')")
    row = conn.execute("SELECT id, outcome FROM prints").fetchone()
    assert row["id"] == "a"
    assert row["outcome"] == "running"


def test_indexes_exist(tmp_path):
    conn = connect(tmp_path / "x.db")
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_prints_started", "idx_prints_outcome"} <= names


def test_filament_requires_a_parent_print(tmp_path):
    conn = connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO print_filaments (print_id, slot) VALUES ('missing', 0)")


def test_filament_cascades_on_print_delete(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute(
        "INSERT INTO prints (id, started_at, outcome) VALUES ('a', 'now', 'running')")
    conn.execute("INSERT INTO print_filaments (print_id, slot) VALUES ('a', 0)")
    conn.execute("DELETE FROM prints WHERE id='a'")
    assert conn.execute("SELECT COUNT(*) FROM print_filaments").fetchone()[0] == 0


def test_duplicate_slot_for_one_print_is_rejected(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute(
        "INSERT INTO prints (id, started_at, outcome) VALUES ('a', 'now', 'running')")
    conn.execute("INSERT INTO print_filaments (print_id, slot) VALUES ('a', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO print_filaments (print_id, slot) VALUES ('a', 0)")
