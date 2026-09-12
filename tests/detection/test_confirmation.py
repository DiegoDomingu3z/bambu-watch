from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.detection.confirmation import FailureDetector
from app.vision.schemas import FailureAnalysis

SESSION = "session-1"


def make_settings(**over) -> Settings:
    base = dict(
        bambu_host="h", bambu_serial="s", bambu_access_code="c",
        anthropic_api_key="k", discord_webhook_url="u",
    )
    base.update(over)
    return Settings(**base)


def analysis(status="failure", confidence=0.9, failure_type="spaghetti",
             severity="high", explanation="x") -> FailureAnalysis:
    return FailureAnalysis(status=status, confidence=confidence,
                           failure_type=failure_type, severity=severity,
                           explanation=explanation)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def detector():
    return FailureDetector(make_settings(), now=Clock())


def test_healthy_result_keeps_normal_interval(detector):
    d = detector.process(analysis(status="healthy", confidence=0.99,
                                  failure_type="none", severity="none"), SESSION)
    assert d.alert is False
    assert d.next_interval == 45


def test_first_high_confidence_failure_does_not_alert(detector):
    d = detector.process(analysis(confidence=0.91), SESSION)
    assert d.alert is False, "one detection must never alert"
    assert d.next_interval == 10, "must switch to suspicious interval"


def test_second_matching_failure_alerts(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    d = detector.process(analysis(confidence=0.94), SESSION)
    assert d.alert is True
    assert d.next_interval == 45, "post-alert returns to normal cadence"


def test_confirmation_below_threshold_does_not_alert(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    d = detector.process(analysis(confidence=0.82), SESSION)
    assert d.alert is False, "0.82 is under the 0.85 confirmation threshold"


def test_healthy_second_check_clears_suspicion(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    d = detector.process(analysis(status="healthy", confidence=0.97,
                                  failure_type="none", severity="none"), SESSION)
    assert d.alert is False
    assert d.next_interval == 45, "cleared suspicion returns to normal interval"


def test_alerting_analysis_is_returned_on_decision(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    d = detector.process(analysis(confidence=0.94), SESSION)
    assert d.analysis is not None, "the notifier needs the analysis to send"
    assert d.analysis.failure_type == "spaghetti"


# --- Regression tests, one per defect in the source brief ---

def test_regression_low_confidence_does_not_pin_suspicious_interval(detector):
    """Defect 4.1: `if confidence < 0.80: return` left pending state set,
    pinning the detector at the 10s interval for the rest of the print.

    Exercises the actual leak path: a high-confidence failure first raises
    suspicion (interval 10), then only low-confidence results arrive. The
    brief's early return never touched state, so the interval stayed at 10
    forever, costing roughly six API calls a minute for the whole print.
    """
    first = detector.process(analysis(confidence=0.91), SESSION)
    assert first.next_interval == 10, "precondition: suspicion is raised"

    d = None
    for _ in range(detector.settings.suspicion_max_checks):
        d = detector.process(analysis(status="uncertain", confidence=0.4,
                                      failure_type="unknown", severity="low"), SESSION)
    assert d.next_interval == 45, "suspicion must not persist indefinitely"
    assert d.alert is False

    # And once cleared, a further low-confidence result stays at normal cadence.
    d = detector.process(analysis(status="uncertain", confidence=0.4,
                                  failure_type="unknown", severity="low"), SESSION)
    assert d.next_interval == 45


def test_low_confidence_with_no_pending_candidate_stays_normal(detector):
    d = detector.process(analysis(status="uncertain", confidence=0.3,
                                  failure_type="unknown", severity="low"), SESSION)
    assert d.next_interval == 45
    assert d.alert is False


def test_regression_disagreeing_failure_type_replaces_candidate(detector):
    """Defect 4.2: a differing failure_type fell through, leaving the stale
    first candidate pending forever."""
    detector.process(analysis(confidence=0.91, failure_type="spaghetti"), SESSION)
    d = detector.process(analysis(confidence=0.93, failure_type="layer_shift"), SESSION)
    assert d.alert is False, "disagreement must not alert"
    assert d.next_interval == 10, "stays suspicious"
    # The new type is now the pending candidate, so it can confirm itself.
    d = detector.process(analysis(confidence=0.95, failure_type="layer_shift"), SESSION)
    assert d.alert is True, "new candidate must be confirmable"


def test_regression_suspicion_times_out(detector):
    """Defect 4.3: nothing ever abandoned an unresolved suspicion."""
    detector.process(analysis(confidence=0.91, failure_type="spaghetti"), SESSION)
    last = None
    for _ in range(detector.settings.suspicion_max_checks + 1):
        last = detector.process(analysis(status="uncertain", confidence=0.5,
                                         failure_type="unknown", severity="low"), SESSION)
    assert last.next_interval == 45
    assert last.alert is False


def test_inconclusive_none_result_is_not_a_failure(detector):
    d = detector.process(None, SESSION)
    assert d.alert is False
    assert d.next_interval == 45


def test_inconclusive_result_does_not_confirm_a_pending_candidate(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    d = detector.process(None, SESSION)
    assert d.alert is False, "a missing analysis must never confirm a failure"


def test_cooldown_suppresses_same_failure_type():
    clock = Clock()
    det = FailureDetector(make_settings(), now=clock)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is True
    clock.advance(minutes=1)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is False, "within cooldown"


def test_cooldown_expires():
    clock = Clock()
    det = FailureDetector(make_settings(), now=clock)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is True
    clock.advance(minutes=16)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is True, "cooldown expired"


def test_different_failure_type_alerts_during_cooldown():
    clock = Clock()
    det = FailureDetector(make_settings(), now=clock)
    det.process(analysis(confidence=0.91, failure_type="spaghetti"), SESSION)
    assert det.process(analysis(confidence=0.94, failure_type="spaghetti"), SESSION).alert
    clock.advance(minutes=2)
    det.process(analysis(confidence=0.92, failure_type="detached_print"), SESSION)
    d = det.process(analysis(confidence=0.93, failure_type="detached_print"), SESSION)
    assert d.alert is True, "cooldown is fingerprinted per failure type"


def test_cooldown_is_scoped_per_session():
    clock = Clock()
    det = FailureDetector(make_settings(), now=clock)
    det.process(analysis(confidence=0.91), SESSION)
    assert det.process(analysis(confidence=0.94), SESSION).alert is True
    det.reset()
    det.process(analysis(confidence=0.91), "session-2")
    assert det.process(analysis(confidence=0.94), "session-2").alert is True


def test_reset_clears_all_state(detector):
    detector.process(analysis(confidence=0.91), SESSION)
    detector.reset()
    d = detector.process(analysis(status="healthy", confidence=0.99,
                                  failure_type="none", severity="none"), SESSION)
    assert d.next_interval == 45
