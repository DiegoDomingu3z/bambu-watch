import pytest

from app.storage.records import (
    METHOD_COMPLETE,
    METHOD_LAYER_FRACTION,
    METHOD_UNKNOWN,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_RUNNING,
    OUTCOME_STOPPED,
    OUTCOME_UNKNOWN,
    compute_consumption,
    determine_outcome,
)


@pytest.mark.parametrize("state,progress,expected", [
    ("FINISH", 100, OUTCOME_COMPLETED),
    ("FINISH", 42, OUTCOME_COMPLETED),
    ("FAILED", 55, OUTCOME_FAILED),
    ("IDLE", 100, OUTCOME_COMPLETED),
    ("IDLE", 99, OUTCOME_COMPLETED),
    ("IDLE", 98, OUTCOME_STOPPED),
    ("IDLE", 0, OUTCOME_STOPPED),
    ("IDLE", None, OUTCOME_UNKNOWN),
    ("PREPARE", 0, OUTCOME_UNKNOWN),
    ("RUNNING", 50, OUTCOME_UNKNOWN),
    (None, None, OUTCOME_UNKNOWN),
    ("SOMETHING_NEW", 50, OUTCOME_UNKNOWN),
])
def test_determine_outcome(state, progress, expected):
    assert determine_outcome(state, progress) == expected


def test_completed_print_consumes_everything_planned():
    grams, method = compute_consumption(340.0, 1240, 1240, OUTCOME_COMPLETED)
    assert grams == pytest.approx(340.0)
    assert method == METHOD_COMPLETE


def test_completed_print_ignores_layer_mismatch():
    grams, method = compute_consumption(340.0, 1200, 1240, OUTCOME_COMPLETED)
    assert grams == pytest.approx(340.0), "a finished print used its whole estimate"
    assert method == METHOD_COMPLETE


def test_stopped_print_uses_layer_fraction():
    grams, method = compute_consumption(340.0, 620, 1240, OUTCOME_STOPPED)
    assert grams == pytest.approx(170.0)
    assert method == METHOD_LAYER_FRACTION


def test_failed_print_uses_layer_fraction():
    grams, method = compute_consumption(1000.0, 883, 1240, OUTCOME_FAILED)
    assert grams == pytest.approx(712.09677, rel=1e-4)
    assert method == METHOD_LAYER_FRACTION


def test_no_planned_grams_yields_none_not_zero():
    grams, method = compute_consumption(None, 620, 1240, OUTCOME_STOPPED)
    assert grams is None, "unknown must not be reported as zero"
    assert method == METHOD_UNKNOWN


def test_missing_layer_data_yields_none():
    assert compute_consumption(340.0, None, 1240, OUTCOME_STOPPED) == (None, METHOD_UNKNOWN)
    assert compute_consumption(340.0, 620, None, OUTCOME_STOPPED) == (None, METHOD_UNKNOWN)


def test_layer_zero_yields_none():
    assert compute_consumption(340.0, 0, 1240, OUTCOME_STOPPED) == (None, METHOD_UNKNOWN)


def test_zero_total_layers_does_not_divide_by_zero():
    assert compute_consumption(340.0, 5, 0, OUTCOME_STOPPED) == (None, METHOD_UNKNOWN)


def test_fraction_is_clamped():
    grams, _ = compute_consumption(340.0, 2000, 1240, OUTCOME_STOPPED)
    assert grams == pytest.approx(340.0), "cannot consume more than planned"


def test_running_outcome_has_no_consumption_yet():
    assert compute_consumption(340.0, 100, 1240, OUTCOME_RUNNING) == (None, METHOD_UNKNOWN)


def test_unknown_outcome_still_estimates_if_layers_are_known():
    grams, method = compute_consumption(340.0, 620, 1240, OUTCOME_UNKNOWN)
    assert grams == pytest.approx(170.0)
    assert method == METHOD_LAYER_FRACTION


def test_method_never_claims_complete_for_a_partial_print():
    for outcome in (OUTCOME_STOPPED, OUTCOME_FAILED, OUTCOME_UNKNOWN):
        _, method = compute_consumption(340.0, 620, 1240, outcome)
        assert method != METHOD_COMPLETE, (
            "an estimate must never be labelled as a measurement"
        )
