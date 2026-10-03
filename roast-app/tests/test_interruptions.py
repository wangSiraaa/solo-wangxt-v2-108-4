"""Unit tests for the interruption ledger and dual-basis phase metrics.

Wall-clock durations never change when a ledger is added; active durations
subtract exactly the union of current closed intervals; every malformed
condition becomes an explicit conflict/null instead of a guessed number.
"""
import math

import pytest

from app.analysis import (
    build_interruptions,
    elapsed_summary,
    phase_metrics,
)


def rec(rid, action, t, iid=1, version=1, superseded=False, **kw):
    return {
        "id": rid,
        "action": action,
        "t_s": t,
        "interval_id": iid,
        "version": version,
        "superseded": superseded,
        "reason": kw.get("reason", "power_outage"),
        "note": "",
        "source": kw.get("source", "manual"),
    }


CHARGE_TO_DROP = [
    {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual", "superseded": False},
    {"id": 2, "event_type": "turning_point", "t_s": 60, "source": "manual", "superseded": False},
    {"id": 3, "event_type": "first_crack_start", "t_s": 480, "source": "manual", "superseded": False},
    {"id": 4, "event_type": "drop", "t_s": 600, "source": "manual", "superseded": False},
]


def test_clean_start_resume_builds_interval_and_status():
    view = build_interruptions(
        [rec(1, "start", 100), rec(2, "resume", 160)], data_end_s=600
    )
    assert view["computable"] is True
    assert view["conflicts"] == []
    assert view["ledger_status"] == "resumed"
    iv = view["intervals"][0]
    assert iv["start_s"] == 100 and iv["end_s"] == 160
    assert iv["duration_s"] == 60
    assert view["total_interrupted_s"] == 60


def test_duplicate_resume_is_conflict_and_not_double_counted():
    # Two resume rows for one open interval: replay closes on the FIRST resume,
    # the second resume is an orphan.  The interval is counted exactly once.
    view = build_interruptions(
        [rec(1, "start", 100), rec(2, "resume", 160), rec(3, "resume", 200)],
        data_end_s=600,
    )
    assert view["computable"] is False
    kinds = [c["kind"] for c in view["conflicts"]]
    assert "orphan_resume" in kinds
    # the assembled interval closes at the first resume (160), never at 200
    assert view["intervals"][0]["end_s"] == 160


def test_resume_without_any_start_is_rejected_as_conflict():
    view = build_interruptions(
        [rec(1, "resume", 100, iid=7)], data_end_s=600
    )
    assert view["computable"] is False
    assert view["conflicts"][0]["kind"] == "orphan_resume"


def test_duplicate_start_while_open_is_conflict():
    view = build_interruptions(
        [rec(1, "start", 100, iid=1), rec(2, "start", 130, iid=2)],
        data_end_s=600,
    )
    assert any(c["kind"] == "duplicate_start" for c in view["conflicts"])
    assert view["computable"] is False


def test_overlapping_intervals_are_conflict():
    view = build_interruptions(
        [
            rec(1, "start", 100, iid=1), rec(2, "resume", 200, iid=1),
            rec(3, "start", 150, iid=2), rec(4, "resume", 250, iid=2),
        ],
        data_end_s=600,
    )
    assert any(c["kind"] == "overlap" for c in view["conflicts"])
    assert view["computable"] is False
    assert view["total_interrupted_s"] is None  # never fabricated


def test_touching_intervals_are_not_overlap_half_open():
    view = build_interruptions(
        [
            rec(1, "start", 100, iid=1), rec(2, "resume", 200, iid=1),
            rec(3, "start", 200, iid=2), rec(4, "resume", 250, iid=2),
        ],
        data_end_s=600,
    )
    assert view["conflicts"] == []
    assert view["total_interrupted_s"] == 150


def test_non_positive_duration_conflict():
    view = build_interruptions(
        [rec(1, "start", 200), rec(2, "resume", 100)], data_end_s=600
    )
    assert any(c["kind"] == "non_positive_duration" for c in view["conflicts"])
    assert view["computable"] is False


def test_open_interval_capped_at_data_end_never_extrapolated():
    view = build_interruptions([rec(1, "start", 550)], data_end_s=600)
    assert view["ledger_status"] == "interrupted"
    iv = view["intervals"][0]
    assert iv["is_open"] and iv["end_s"] is None
    assert view["total_interrupted_s"] == 50  # 600 - 550, capped


def test_open_interval_without_data_end_gives_null_not_guess():
    view = build_interruptions([rec(1, "start", 550)], data_end_s=None)
    assert view["total_interrupted_s"] is None


def test_terminate_with_open_interval_closes_and_ends():
    view = build_interruptions(
        [rec(1, "start", 500), rec(2, "terminate", 560, iid=1)], data_end_s=600
    )
    assert view["ledger_status"] == "ended"
    assert view["intervals"][0]["close_action"] == "terminate"


def test_terminate_without_open_interval_is_normal_end():
    view = build_interruptions(
        [rec(1, "terminate", 600, iid=None)], data_end_s=600
    )
    assert view["ledger_status"] == "ended"
    assert view["intervals"] == []
    assert view["conflicts"] == []


def test_superseded_version_rows_are_ignored_in_current_view():
    view = build_interruptions(
        [
            rec(1, "start", 100, iid=1, version=1, superseded=True),
            rec(2, "resume", 130, iid=1, version=1, superseded=True),
            rec(3, "start", 100, iid=1, version=2),
            rec(4, "resume", 180, iid=1, version=2),
        ],
        data_end_s=600,
    )
    assert view["conflicts"] == []
    assert view["intervals"][0]["version"] == 2
    assert view["intervals"][0]["duration_s"] == 80  # new version, not old 30


# ---------------- phase metrics dual basis ----------------

def test_wall_metrics_unchanged_active_subtracts_interval():
    ledger = build_interruptions(
        [rec(1, "start", 200), rec(2, "resume", 260)], data_end_s=600
    )
    m = phase_metrics(CHARGE_TO_DROP, ledger, data_end_s=600)
    # wall clock: exactly the historical numbers
    assert m["total_s"] == 600 and m["development_s"] == 120
    assert m["development_ratio"] == 0.2
    # active: the 60 s interruption sits inside maillard (60 -> 480)
    assert m["total_active_s"] == 540
    assert m["maillard_active_s"] == 360
    assert m["development_active_s"] == 120  # no overlap with development
    assert math.isclose(m["development_ratio_active"], round(120 / 540, 4), abs_tol=1e-9)
    assert m["active_metrics_status"] == "ok"


def test_active_dtr_null_when_anchor_inside_interruption():
    # interruption covers the first-crack anchor at 480 ([470,490))
    ledger = build_interruptions(
        [rec(1, "start", 470), rec(2, "resume", 490)], data_end_s=600
    )
    m = phase_metrics(CHARGE_TO_DROP, ledger, data_end_s=600)
    assert m["development_active_s"] is None
    assert m["maillard_active_s"] is None  # fc anchors maillard too
    assert m["development_ratio_active"] is None
    # wall-clock values remain fully valid and visible
    assert m["development_s"] == 120 and m["development_ratio"] == 0.2
    assert m["active_metrics_status"] == "partial"
    assert m["active_blockers"][0]["reason"] == "anchor_inside_interruption"


def test_anchor_exactly_at_resume_instant_is_active_half_open():
    ledger = build_interruptions(
        [rec(1, "start", 450), rec(2, "resume", 480)], data_end_s=600
    )  # fc anchor t=480 == end -> active
    m = phase_metrics(CHARGE_TO_DROP, ledger, data_end_s=600)
    assert m["development_active_s"] == 120
    assert m["maillard_active_s"] == 420 - 30


def test_anchor_exactly_at_start_instant_is_inside():
    ledger = build_interruptions(
        [rec(1, "start", 480), rec(2, "resume", 500)], data_end_s=600
    )
    m = phase_metrics(CHARGE_TO_DROP, ledger, data_end_s=600)
    assert m["development_active_s"] is None


def test_conflict_ledger_nulls_all_active_but_keeps_wall():
    ledger = build_interruptions(
        [rec(1, "resume", 100, iid=5)], data_end_s=600
    )
    m = phase_metrics(CHARGE_TO_DROP, ledger, data_end_s=600)
    assert m["active_metrics_status"] == "conflict"
    assert m["total_active_s"] is None
    assert m["development_ratio_active"] is None
    assert m["total_s"] == 600 and m["development_ratio"] == 0.2


def test_no_ledger_means_unavailable_not_zero():
    m = phase_metrics(CHARGE_TO_DROP, None, data_end_s=600)
    assert m["total_s"] == 600
    assert m["total_active_s"] is None
    assert m["active_metrics_status"] == "unavailable"


def test_elapsed_summary_wall_and_active_for_completed_batch():
    ledger = build_interruptions(
        [rec(1, "start", 200), rec(2, "resume", 260)], data_end_s=600
    )
    e = elapsed_summary(CHARGE_TO_DROP, ledger, 600)
    assert e["wall_elapsed_s"] == 600
    assert e["active_elapsed_s"] == 540
    assert e["excluded_interrupted_s"] == 60
    assert e["ended_with_drop"] is True


def test_elapsed_summary_ongoing_open_interval_capped_at_data_end():
    events = [
        {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual", "superseded": False},
    ]
    ledger = build_interruptions([rec(1, "start", 300)], data_end_s=400)
    e = elapsed_summary(events, ledger, 400)
    assert e["wall_elapsed_s"] == 400
    assert e["active_elapsed_s"] == 300
    assert e["open_interval_id"] == 1
