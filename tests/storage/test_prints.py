from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.storage.database import connect
from app.storage.prints import FilamentRow, PrintRepository
from app.storage.records import (
    METHOD_COMPLETE,
    METHOD_LAYER_FRACTION,
    OUTCOME_COMPLETED,
    OUTCOME_RUNNING,
    OUTCOME_STOPPED,
    OUTCOME_UNKNOWN,
)

START = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)


def make_settings(tmp_path, **over) -> Settings:
    base = dict(bambu_host="h", bambu_serial="s", bambu_access_code="c",
                anthropic_api_key="k", discord_webhook_url="u", data_dir=tmp_path)
    base.update(over)
    return Settings(**base)


@pytest.fixture
def repo(tmp_path):
    s = make_settings(tmp_path)
    return PrintRepository(connect(s.db_path), s)


def insert(repo, print_id="p1", name="Mask_Final_v17"):
    repo.insert_running(print_id, name, START, "data/sessions/x")
    return print_id


def finalize(repo, print_id="p1", **over):
    kw = dict(final_state="FINISH", ended_at=START + timedelta(hours=4),
              progress=100, layer=1240, total_layers=1240,
              planned_grams=340.0, planned_meters=113.2,
              checks=320, alerts=0, input_tokens=496000, output_tokens=96000)
    kw.update(over)
    return repo.finalize(print_id, **kw)


def test_insert_running_creates_row(repo):
    insert(repo)
    row = repo.get("p1")
    assert row["outcome"] == OUTCOME_RUNNING
    assert row["file_name"] == "Mask_Final_v17"
    assert row["ended_at"] is None
    assert row["cost_per_gram"] == pytest.approx(0.013)
    assert row["vision_model"] == "claude-sonnet-5"
    assert row["session_dir"] == "data/sessions/x"


def test_insert_is_idempotent_on_restart(repo):
    insert(repo)
    insert(repo)
    assert repo.get("p1") is not None


def test_finalize_completed_print(repo):
    insert(repo)
    assert finalize(repo) == OUTCOME_COMPLETED
    row = repo.get("p1")
    assert row["outcome"] == OUTCOME_COMPLETED
    assert row["duration_seconds"] == 4 * 3600
    assert row["planned_cost"] == pytest.approx(4.42)
    assert row["consumed_grams"] == pytest.approx(340.0)
    assert row["consumed_cost"] == pytest.approx(4.42)
    assert row["consumption_method"] == METHOD_COMPLETE
    assert row["checks"] == 320
    assert row["input_tokens"] == 496000


def test_finalize_stopped_print_estimates_consumption(repo):
    insert(repo)
    assert finalize(repo, final_state="IDLE", progress=50, layer=620) == OUTCOME_STOPPED
    row = repo.get("p1")
    assert row["consumed_grams"] == pytest.approx(170.0)
    assert row["consumed_cost"] == pytest.approx(2.21)
    assert row["consumption_method"] == METHOD_LAYER_FRACTION
    assert row["planned_cost"] == pytest.approx(4.42), "planned is unchanged"


def test_finalize_failed_print(repo):
    insert(repo)
    assert finalize(repo, final_state="FAILED", progress=71, layer=883,
                    total_layers=1240) == "failed"
    row = repo.get("p1")
    assert row["consumed_grams"] == pytest.approx(242.15, rel=1e-3)


def test_finalize_without_slice_info_records_no_material(repo):
    insert(repo)
    finalize(repo, planned_grams=None, planned_meters=None)
    row = repo.get("p1")
    assert row["planned_grams"] is None
    assert row["planned_cost"] is None
    assert row["consumed_grams"] is None, "unknown must not read as zero"
    assert row["consumed_cost"] is None


def test_cost_per_gram_is_snapshotted_per_print(tmp_path):
    s1 = make_settings(tmp_path)
    repo1 = PrintRepository(connect(s1.db_path), s1)
    insert(repo1)
    finalize(repo1)

    s2 = make_settings(tmp_path, spool_cost=26.0)
    repo2 = PrintRepository(connect(s2.db_path), s2)
    assert repo2.get("p1")["cost_per_gram"] == pytest.approx(0.013), (
        "history must not shift when the spool price changes"
    )
    assert repo2.get("p1")["planned_cost"] == pytest.approx(4.42)
    insert(repo2, "p2")
    assert repo2.get("p2")["cost_per_gram"] == pytest.approx(0.026)
    finalize(repo2, "p2")
    assert repo2.get("p2")["planned_cost"] == pytest.approx(8.84)


def test_save_filaments_records_every_slot(repo):
    insert(repo)
    repo.save_filaments("p1", [
        FilamentRow(0, "PLA", "#FF0000", 120.0, 40.0),
        FilamentRow(1, "PLA", "#00FF00", 95.5, 31.8),
        FilamentRow(2, "PETG", "#0000FF", 80.0, 26.0),
        FilamentRow(3, "PLA", "#FFFFFF", 44.5, 15.4),
    ])
    rows = repo.filaments("p1")
    assert len(rows) == 4
    assert [r["slot"] for r in rows] == [0, 1, 2, 3]
    assert {r["color"] for r in rows} == {"#FF0000", "#00FF00", "#0000FF", "#FFFFFF"}
    assert sum(r["used_grams"] for r in rows) == pytest.approx(340.0)
    assert rows[2]["filament_type"] == "PETG"


def test_save_filaments_replaces_rather_than_duplicates(repo):
    insert(repo)
    repo.save_filaments("p1", [FilamentRow(0, "PLA", "#FF0000", 120.0, 40.0)])
    repo.save_filaments("p1", [FilamentRow(0, "PLA", "#FF0000", 130.0, 43.0)])
    rows = repo.filaments("p1")
    assert len(rows) == 1
    assert rows[0]["used_grams"] == pytest.approx(130.0)


def test_external_spool_uses_slot_minus_one(repo):
    insert(repo)
    repo.save_filaments("p1", [FilamentRow(-1, "PLA", "#123456", 50.0, 17.0)])
    assert repo.filaments("p1")[0]["slot"] == -1


def test_save_filaments_accepts_an_empty_list(repo):
    insert(repo)
    repo.save_filaments("p1", [])
    assert repo.filaments("p1") == []


def test_reconcile_stale_marks_orphaned_running_rows(repo):
    insert(repo, "p1")
    insert(repo, "p2")
    finalize(repo, "p2")
    assert repo.reconcile_stale() == 1
    assert repo.get("p1")["outcome"] == OUTCOME_UNKNOWN
    assert repo.get("p2")["outcome"] == OUTCOME_COMPLETED


def test_reconcile_stale_is_a_noop_when_clean(repo):
    insert(repo)
    finalize(repo)
    assert repo.reconcile_stale() == 0


def test_set_slice_source(repo):
    insert(repo)
    repo.set_slice_source("p1", "unavailable")
    assert repo.get("p1")["slice_info_source"] == "unavailable"


def test_get_unknown_print_returns_none(repo):
    assert repo.get("nope") is None


# --- api cost and the finishing image ---

def test_finalize_records_api_cost_and_its_rates(repo):
    insert(repo)
    finalize(repo, input_tokens=496_000, output_tokens=96_000)
    row = repo.get("p1")
    # 496k in at 2.0/MTok plus 96k out at 10.0/MTok
    assert row["api_cost"] == pytest.approx(496_000 * 2.0 / 1e6 + 96_000 * 10.0 / 1e6)
    assert row["api_cost"] == pytest.approx(1.952)
    assert row["api_input_rate"] == pytest.approx(2.0)
    assert row["api_output_rate"] == pytest.approx(10.0)


def test_api_cost_follows_configured_rates(tmp_path):
    s = make_settings(tmp_path, vision_input_cost_per_mtok=1.0,
                      vision_output_cost_per_mtok=5.0)
    r = PrintRepository(connect(s.db_path), s)
    insert(r)
    finalize(r, input_tokens=1_000_000, output_tokens=100_000)
    row = r.get("p1")
    assert row["api_cost"] == pytest.approx(1.0 + 0.5)
    assert row["api_input_rate"] == pytest.approx(1.0)


def test_api_cost_is_zero_when_no_tokens_were_spent(repo):
    insert(repo)
    finalize(repo, input_tokens=0, output_tokens=0)
    assert repo.get("p1")["api_cost"] == pytest.approx(0.0)


def test_total_cost_combines_filament_and_tokens(repo):
    insert(repo)
    finalize(repo, input_tokens=100_000, output_tokens=20_000)
    row = repo.conn.execute(
        "SELECT filament_cost, api_cost, total_cost FROM print_summary WHERE id='p1'"
    ).fetchone()
    assert row["filament_cost"] == pytest.approx(4.42)
    assert row["api_cost"] == pytest.approx(0.4)
    assert row["total_cost"] == pytest.approx(4.82)


def test_save_final_frame_stores_blob_and_path(repo):
    insert(repo)
    repo.save_final_frame("p1", b"\xff\xd8body\xff\xd9", "data/sessions/x/frames/0042.jpg")
    row = repo.get("p1")
    assert row["final_frame_jpeg"] == b"\xff\xd8body\xff\xd9"
    assert row["final_frame_path"].endswith("0042.jpg")
    assert repo.final_frame("p1") == b"\xff\xd8body\xff\xd9"


def test_save_final_frame_accepts_a_path_with_no_blob(repo):
    insert(repo)
    repo.save_final_frame("p1", None, "some/path.jpg")
    row = repo.get("p1")
    assert row["final_frame_jpeg"] is None
    assert row["final_frame_path"] == "some/path.jpg"


def test_final_frame_of_unknown_print_is_none(repo):
    assert repo.final_frame("nope") is None


def test_finalize_does_not_disturb_a_stored_image(repo):
    insert(repo)
    repo.save_final_frame("p1", b"\xff\xd8x\xff\xd9", "p.jpg")
    finalize(repo)
    assert repo.final_frame("p1") == b"\xff\xd8x\xff\xd9"
