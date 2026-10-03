"""Unit tests for the interruption ledger: wall-clock vs active roasting
time, structural conflicts, versioned corrections.

A probe dropout (NULL sample) must never be treated as an interruption;
active time is derived ONLY from the operator ledger."""
import math

import pytest

from app.analysis import (
    RoRConfig,
    build_series,
    current_interruption_intervals,
    interruption_duration_in_span,
    interruption_versions,
    phase_metrics,
)


ANCHORS = [
    {"id": 1, "event_type": "charge", "t_s": 0, "source": "manual",
     "superseded": False, "created_at": "2026-01-01T00:00:00"},
    {"id": 2, "event_type": "turning_point", "t_s": 60, "source": "manual",
     "superseded": False, "created_at": "2026-01-01T00:00:00"},
    {"id": 3, "event_type": "first_crack_start", "t_s": 480, "source": "manual",
     "superseded": False, "created_at": "2026-01-01T00:00:00"},
    {"id": 4, "event_type": "first_crack_end", "t_s": 535, "source": "manual",
     "superseded": False, "created_at": "2026-01-01T00:00:00"},
    {"id": 5, "event_type": "drop", "t_s": 600, "source": "manual",
     "superseded": False, "created_at": "2026-01-01T00:00:00"},
]


def _iv(interval_id, version, action, t_s, created_at, superseded=False,
        superseded_by_id=None, reason="power_cut"):
    return {
        "id": hash((interval_id, version, action)) & 0xFFFF,
        "interval_id": interval_id,
        "version": version,
        "action": action,
        "t_s": t_s,
        "reason": reason,
        "source": "manual",
        "created_by": "op",
        "note": "",
        "superseded": superseded,
        "superseded_by_id": superseded_by_id,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# acceptance ① legal interruption + resume: both bases reported
# ---------------------------------------------------------------------------

def test_wall_clock_and_active_time_both_reported_after_resume():
    # heat off 300 -> 360 (60 s, inside maillard)
    records = [
        _iv("g1", 1, "start", 300, "2026-01-01T00:05:00"),
        _iv("g1", 1, "resume", 360, "2026-01-01T00:06:00", reason="fixed"),
    ]
    m = phase_metrics(ANCHORS, records)
    # wall-clock unchanged
    assert m["total_s"] == 600
    assert m["drying_s"] == 60
    assert m["maillard_s"] == 420
    assert m["development_s"] == 120
    assert math.isclose(m["development_ratio"], 0.2)
    # active: maillard loses 60 s, drying/development untouched
    assert m["drying_active_s"] == 60
    assert m["maillard_active_s"] == 360
    assert m["development_active_s"] == 120
    assert m["total_active_s"] == 540
    assert m["total_interrupted_s"] == 60
    assert math.isclose(m["development_ratio_active"], 120 / 540, abs_tol=1e-4)
    assert m["active_time_computable"] is True
    assert m["interruption_basis"]["conflicts"] == []


def test_interruption_touching_phase_boundary_is_split_not_conflict():
    # stop exactly at turning point, resume at 320
    records = [
        _iv("g1", 1, "start", 60, "2026-01-01T00:01:00"),
        _iv("g1", 1, "resume", 320, "2026-01-01T00:05:00"),
    ]
    m = phase_metrics(ANCHORS, records)
    assert m["drying_active_s"] == 60        # interruption starts at the boundary
    assert m["maillard_active_s"] == 160     # 420 - 260
    assert m["active_time_computable"] is True
    notes = m["interruption_basis"]["anchor_notes"]
    assert any(n["anchor"] == "turning_point" for n in notes)


def test_multiple_closed_interruptions_union_duration():
    records = [
        _iv("g1", 1, "start", 100, "2026-01-01T00:01:00"),
        _iv("g1", 1, "resume", 140, "2026-01-01T00:02:00"),
        _iv("g2", 1, "start", 500, "2026-01-01T00:08:00"),
        _iv("g2", 1, "resume", 520, "2026-01-01T00:09:00"),
    ]
    m = phase_metrics(ANCHORS, records)
    assert m["total_interrupted_s"] == 60
    assert m["total_active_s"] == 540
    # g2 falls entirely inside development (480-600)
    assert m["development_active_s"] == 100


# ---------------------------------------------------------------------------
# acceptance ② duplicate resume cannot double count; no-start resume rejected
# ---------------------------------------------------------------------------

def test_duplicate_resume_does_not_double_count():
    # Two resumes against the SAME start — structurally invalid ledger.
    records = [
        _iv("g1", 1, "start", 300, "t1"),
        _iv("g1", 1, "resume", 360, "t2"),
        _iv("g1", 1, "resume", 400, "t3"),
    ]
    interp = current_interruption_intervals(records)
    codes = [c["code"] for c in interp["conflicts"]]
    assert "invalid_ledger_structure" in codes
    assert interp["computable"] is False
    m = phase_metrics(ANCHORS, records)
    # wall clock still given; active values withheld, not fabricated
    assert m["total_s"] == 600
    assert m["total_active_s"] is None
    assert m["development_ratio_active"] is None


def test_resume_without_start_is_conflict_and_not_guessed():
    records = [
        {"id": 9, "interval_id": "ghost", "version": 1, "action": "resume",
         "t_s": 300, "reason": "", "source": "manual", "created_by": "op",
         "note": "", "superseded": False, "superseded_by_id": None,
         "created_at": "t1"},
    ]
    interp = current_interruption_intervals(records)
    assert interp["intervals"] == []
    assert any(c["code"] == "invalid_ledger_structure" for c in interp["conflicts"])
    assert interp["computable"] is False
    m = phase_metrics(ANCHORS, records)
    assert m["total_active_s"] is None


def test_close_before_start_is_conflict():
    records = [
        _iv("g1", 1, "start", 400, "t2"),
        _iv("g1", 1, "resume", 300, "t1"),
    ]
    interp = current_interruption_intervals(records)
    assert interp["computable"] is False


def test_open_interruption_makes_active_time_null_but_wall_kept():
    records = [_iv("g1", 1, "start", 550, "t1")]
    m = phase_metrics(ANCHORS, records, active_horizon_s=600)
    inter = m["open_interruption"]
    assert inter is not None
    assert inter["start_s"] == 550
    assert m["total_s"] == 600          # wall clock known
    assert m["total_active_s"] is None  # cannot know resume time
    assert m["development_active_s"] is None
    assert m["active_time_computable"] is False


# ---------------------------------------------------------------------------
# acceptance ③ backdated correction: new version, old interval/metrics kept
# ---------------------------------------------------------------------------

def test_correction_version_replaces_current_but_keeps_history():
    records = [
        _iv("g1", 1, "start", 300, "2026-01-01T00:05:00"),
        _iv("g1", 1, "resume", 360, "2026-01-01T00:06:00"),
    ]
    before = phase_metrics(ANCHORS, records)
    assert before["total_active_s"] == 540

    # corrected version: actual heat-off was 310 -> 340 (30 s)
    # v1 rows are kept (marked superseded) and v2 rows appended.
    records = [
        _iv("g1", 1, "start", 300, "2026-01-01T00:05:00", superseded=True,
            superseded_by_id=100),
        _iv("g1", 1, "resume", 360, "2026-01-01T00:06:00", superseded=True,
            superseded_by_id=100),
        _iv("g1", 2, "start", 310, "2026-01-01T01:00:00"),
        _iv("g1", 2, "resume", 340, "2026-01-01T01:01:00"),
    ]
    after = phase_metrics(ANCHORS, records)
    assert after["total_active_s"] == 570          # current uses v2
    assert after["total_interrupted_s"] == 30

    # old version still reconstructable exactly (as_of after v1 resume but
    # before the v2 correction rows were written)
    as_of_v1 = phase_metrics(ANCHORS, records, as_of="2026-01-01T00:06:01")
    assert as_of_v1["total_active_s"] == 540

    versions = interruption_versions(records)
    vmap = {(v["interval_id"], v["version"]): v for v in versions}
    assert vmap[("g1", 1)]["superseded"] is True
    assert vmap[("g1", 1)]["start_s"] == 300
    assert vmap[("g1", 2)]["superseded"] is False
    assert vmap[("g1", 2)]["start_s"] == 310


def test_correction_reopening_interval_produces_open_state():
    records = [
        _iv("g1", 1, "start", 300, "t1", superseded=True, superseded_by_id=100),
        _iv("g1", 1, "resume", 360, "t2", superseded=True, superseded_by_id=100),
        _iv("g1", 2, "start", 320, "t3"),
    ]
    interp = current_interruption_intervals(records)
    assert interp["open"] is True
    assert interp["batch_status"] == "interrupted"
    assert interp["intervals"][0]["start_s"] == 320


# ---------------------------------------------------------------------------
# acceptance ④ overlaps / anchor inside band: flagged, no fabricated DTR
# ---------------------------------------------------------------------------

def test_overlapping_intervals_flagged_and_active_dtr_withheld():
    records = [
        _iv("g1", 1, "start", 300, "t1"),
        _iv("g1", 1, "resume", 400, "t2"),
        _iv("g2", 1, "start", 350, "t3"),
        _iv("g2", 1, "resume", 450, "t4"),
    ]
    interp = current_interruption_intervals(records)
    assert any(c["code"] == "overlapping_interruptions" for c in interp["conflicts"])
    assert interp["computable"] is False
    m = phase_metrics(ANCHORS, records)
    assert m["development_ratio"] == 0.2            # wall DTR still honest
    assert m["development_ratio_active"] is None    # active DTR refused
    assert m["total_active_s"] is None


def test_touching_intervals_are_not_overlap():
    # g1 ends exactly when g2 starts: legal
    records = [
        _iv("g1", 1, "start", 300, "t1"),
        _iv("g1", 1, "resume", 350, "t2"),
        _iv("g2", 1, "start", 350, "t3"),
        _iv("g2", 1, "resume", 400, "t4"),
    ]
    interp = current_interruption_intervals(records)
    assert interp["computable"] is True
    assert interruption_duration_in_span(interp["intervals"], 0, 600) == 100


def test_anchor_strictly_inside_band_flags_phase_and_withholds_active():
    # band 470 -> 500 contains first_crack_start at 480
    records = [
        _iv("g1", 1, "start", 470, "t1"),
        _iv("g1", 1, "resume", 500, "t2"),
    ]
    m = phase_metrics(ANCHORS, records)
    codes = [c["code"] for c in m["interruption_basis"]["conflicts"]]
    assert "phase_anchor_inside_interruption" in codes
    # maillard and development share the tainted anchor
    assert m["maillard_active_s"] is None
    assert m["development_active_s"] is None
    assert m["development_ratio_active"] is None
    # unrelated phase and wall clock still fine
    assert m["drying_active_s"] == 60
    assert m["development_ratio"] == 0.2


def test_anchor_exactly_on_boundary_is_allowed():
    records = [
        _iv("g1", 1, "start", 480, "t1"),
        _iv("g1", 1, "resume", 510, "t2"),
    ]
    interp = current_interruption_intervals(
        records, event_times={"first_crack_start": 480}
    )
    assert interp["conflicts"] == []
    assert interp["anchor_notes"]


# ---------------------------------------------------------------------------
# raw samples / probe dropouts are never interruptions
# ---------------------------------------------------------------------------

def test_probe_dropout_does_not_become_interruption():
    samples = [
        {"t_s": 0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
        {"t_s": 10, "bean_temp_c": None, "env_temp_c": None},
        {"t_s": 20, "bean_temp_c": 150.0, "env_temp_c": 192.0},
    ]
    out = build_series(samples, ror_cfg=RoRConfig(), max_gap_fill_s=45)
    assert out["interruption_bands"] == []
    assert out["interruption_ledger"]["status"] == "in_progress"
    # the dropout IS recorded in the gap audit, as a gap — not an interruption
    assert out["missing_segments"]


def test_build_series_bands_from_ledger_leave_raw_untouched():
    samples = [
        {"t_s": 0, "bean_temp_c": 180.0, "env_temp_c": 190.0},
        {"t_s": 300, "bean_temp_c": 150.0, "env_temp_c": 200.0},
        {"t_s": 330, "bean_temp_c": None, "env_temp_c": 201.0},
        {"t_s": 360, "bean_temp_c": 152.0, "env_temp_c": 202.0},
        {"t_s": 600, "bean_temp_c": 208.0, "env_temp_c": 215.0},
    ]
    records = [
        _iv("g1", 1, "start", 300, "t1"),
        _iv("g1", 1, "resume", 360, "t2"),
    ]
    out = build_series(samples, ror_cfg=RoRConfig(), max_gap_fill_s=45,
                       interruption_records=records)
    band = out["interruption_bands"][0]
    assert band["start_s"] == 300 and band["end_s"] == 360
    # raw points unchanged — the NULL is still NULL, no point removed
    raw = [(p["t_s"], p["bean_temp_c"]) for p in out["raw_points"]]
    assert raw == [(0, 180.0), (300, 150.0), (330, None), (360, 152.0), (600, 208.0)]


def test_open_band_extends_to_horizon_only_for_display():
    records = [_iv("g1", 1, "start", 500, "t1")]
    interp = current_interruption_intervals(records)
    from app.analysis import interruption_bands
    b = interruption_bands(interp["intervals"], open_until_s=600)
    assert b[0]["end_s"] == 600 and b[0]["open"] is True
    # but active time through the open span is still unknowable
    assert interruption_duration_in_span(interp["intervals"], 0, 600) is None


# ---------------------------------------------------------------------------
# status derivation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], "in_progress"),
        ([("start", 100)], "interrupted"),
        ([("start", 100), ("resume", 200)], "resumed"),
        ([("start", 100), ("terminate", 600)], "ended"),
        ([("terminate", 600)], "ended"),  # batch-level end, no interval
    ],
)
def test_batch_status_derivation(rows, expected):
    has_start = any(a == "start" for a, _ in rows)
    records = []
    for i, (a, t) in enumerate(rows):
        # A terminate only shares an episode when a start exists; otherwise it
        # is a batch-level end marker with interval_id NULL.
        gid = "g1" if has_start else None
        records.append(
            {"id": i, "interval_id": gid, "version": 1, "action": a, "t_s": t,
             "reason": "", "source": "manual", "created_by": "op", "note": "",
             "superseded": False, "superseded_by_id": None, "created_at": f"t{i}"}
        )
    interp = current_interruption_intervals(records)
    assert interp["batch_status"] == expected
