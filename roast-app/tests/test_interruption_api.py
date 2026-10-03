"""End-to-end tests for the interruption ledger API and dual-basis metrics.

Covers the five acceptance scenarios:
 1 legal start -> resume shows wall AND active durations, raw curve intact;
 2 duplicate resume never double-counts, resume without open interval 409;
 3 post-hoc correction inserts a new version: old version retained, current
   metrics recomputed;
 4 overlapping / key-event-covering intervals are flagged, no fake active DTR;
 5 export + independent recompute agree on boundaries, time basis and results.
"""
import os
import sys

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app import models  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_ledger():
    """The client fixture is session-scoped and the two demo batches are
    shared, so each ledger test starts from an empty ledger."""
    with Session(models.engine) as s:
        s.execute(delete(models.InterruptRecord))
        for b in s.query(models.Batch).all():
            b.status = models.BATCH_IN_PROGRESS
        s.commit()
    yield


def _seed(client):
    batches = client.post("/api/seed").json()
    return batches[0]["id"]


def _start(client, aid, t=200.0, reason="power_outage"):
    return client.post(f"/api/batches/{aid}/interruptions", json={
        "action": "start", "t_s": t, "reason": reason,
        "source": "manual", "created_by": "tester"})


def _resume(client, aid, t=260.0):
    return client.post(f"/api/batches/{aid}/interruptions", json={
        "action": "resume", "t_s": t, "reason": "power_restored",
        "source": "manual", "created_by": "tester"})


def _series(client, aid, **params):
    return client.get(f"/api/batches/{aid}/series", params=params).json()


# ---------------------------------------------------------------- 1

def test_legal_interruption_then_resume_shows_both_clocks_and_raw_intact(client):
    aid = _seed(client)
    before = _series(client, aid)
    raw_before = [(p["t_s"], p["bean_temp_c"], p["env_temp_c"])
                  for p in before["series"]["raw_points"]]

    assert _start(client, aid, 200.0).status_code == 200
    # status is interrupted while open; wall keeps running, active subtracts the
    # open interval capped at the last measured sample (no extrapolation)
    open_view = _series(client, aid)
    assert open_view["batch"]["status"] == "interrupted"
    assert open_view["interruptions"]["open_interval_id"] == 1
    assert open_view["elapsed"]["active_elapsed_s"] is not None
    assert open_view["elapsed"]["active_elapsed_s"] < open_view["elapsed"]["wall_elapsed_s"]

    assert _resume(client, aid, 260.0).status_code == 200
    after = _series(client, aid)
    assert after["batch"]["status"] == "resumed"

    iv = after["interruptions"]["intervals"][0]
    assert iv["start_s"] == 200 and iv["end_s"] == 260 and iv["duration_s"] == 60

    # both clocks side by side at page level
    assert after["elapsed"]["wall_elapsed_s"] is not None
    assert after["elapsed"]["excluded_interrupted_s"] == 60
    assert after["elapsed"]["active_elapsed_s"] == \
        after["elapsed"]["wall_elapsed_s"] - 60

    # wall metrics untouched; active metrics subtract the 60 s
    m = after["metrics"]
    assert m["active_metrics_status"] == "ok"
    assert m["total_active_s"] == m["total_s"] - 60
    for row in m["duration_table"]:
        if row["wall_s"] is not None:
            assert row["active_s"] is not None

    # raw curve is complete and byte-for-byte identical
    raw_after = [(p["t_s"], p["bean_temp_c"], p["env_temp_c"])
                 for p in after["series"]["raw_points"]]
    assert raw_after == raw_before


# ---------------------------------------------------------------- 2

def test_resume_without_open_interval_is_rejected(client):
    aid = _seed(client)
    r = _resume(client, aid, 260.0)
    assert r.status_code == 409
    assert "没有处于打开状态" in r.json()["detail"]
    # nothing was written: ledger empty, batch still in_progress
    d = _series(client, aid)
    assert d["interruptions"]["records"] == []
    assert d["batch"]["status"] == "in_progress"


def test_duplicate_start_rejected_and_duplicate_resume_not_double_counted(client):
    aid = _seed(client)
    assert _start(client, aid, 200.0).status_code == 200
    dup = _start(client, aid, 220.0)
    assert dup.status_code == 409

    assert _resume(client, aid, 260.0).status_code == 200
    # a second resume is rejected (no open interval any more), not counted again
    dup_resume = _resume(client, aid, 320.0)
    assert dup_resume.status_code == 409

    d = _series(client, aid)
    ivs = d["interruptions"]["intervals"]
    assert len(ivs) == 1
    assert ivs[0]["duration_s"] == 60  # exactly once
    assert d["elapsed"]["excluded_interrupted_s"] == 60
    current_records = d["interruptions"]["records"]
    assert len(current_records) == 2


def test_end_batch_via_terminate_closes_open_interval(client):
    aid = _seed(client)
    _start(client, aid, 500.0)
    r = client.post(f"/api/batches/{aid}/interruptions", json={
        "action": "terminate", "t_s": 560.0, "source": "manual"})
    assert r.status_code == 200
    d = _series(client, aid)
    assert d["batch"]["status"] == "ended"
    assert d["interruptions"]["intervals"][0]["close_action"] == "terminate"
    # no further ledger actions on an ended batch
    assert _start(client, aid, 570.0).status_code == 409


def test_resume_before_start_time_rejected(client):
    aid = _seed(client)
    _start(client, aid, 200.0)
    r = _resume(client, aid, 120.0)
    assert r.status_code == 422


# ---------------------------------------------------------------- 3

def test_post_hoc_correction_creates_new_version_keeps_history(client):
    aid = _seed(client)
    _start(client, aid, 200.0)
    _resume(client, aid, 260.0)  # operator later realises it was 180 -> 300

    # metrics under v1
    v1 = _series(client, aid)
    assert v1["interruptions"]["intervals"][0]["version"] == 1
    assert v1["elapsed"]["excluded_interrupted_s"] == 60

    r = client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id=1",
        json={
            "start_s": 180.0, "end_s": 300.0, "close_action": "resume",
            "reason": "safety_inspection", "source": "post_hoc",
            "created_by": "supervisor", "note": "后补修正：安全员核对停电单",
        },
    )
    assert r.status_code == 200, r.text

    cur = _series(client, aid)
    iv = cur["interruptions"]["intervals"][0]
    assert iv["version"] == 2
    assert iv["start_s"] == 180 and iv["end_s"] == 300 and iv["duration_s"] == 120
    assert cur["elapsed"]["excluded_interrupted_s"] == 120  # current = new version
    assert cur["batch"]["status"] == "resumed"

    # history: old version retained, superseded, pointing at the new start
    hist = client.get(
        f"/api/batches/{aid}/interruptions?include_history=true").json()
    v1_rows = [x for x in hist if x["version"] == 1]
    v2_rows = [x for x in hist if x["version"] == 2]
    assert len(v1_rows) == 2 and all(x["superseded"] for x in v1_rows)
    assert len(v2_rows) == 2 and all(not x["superseded"] for x in v2_rows)
    new_start_id = next(x["id"] for x in v2_rows if x["action"] == "start")
    assert all(x["superseded_by_id"] == new_start_id for x in v1_rows)

    # series with history includes both versions' raw rows
    hist_view = _series(client, aid, include_history="true")
    ids = {x["id"] for x in hist_view["interruptions"]["records"]}
    assert ids == {x["id"] for x in hist}


def test_correct_unknown_interval_404(client):
    aid = _seed(client)
    r = client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id=99",
        json={"start_s": 10, "end_s": 20, "close_action": "resume"})
    assert r.status_code == 404


def test_correct_inverted_interval_422(client):
    aid = _seed(client)
    _start(client, aid, 200.0)
    _resume(client, aid, 260.0)
    r = client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id=1",
        json={"start_s": 260, "end_s": 200, "close_action": "resume"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 4

def test_overlapping_correction_refused_not_silently_fixed(client):
    aid = _seed(client)
    _start(client, aid, 100.0);  _resume(client, aid, 200.0)
    _start(client, aid, 300.0);  _resume(client, aid, 400.0)
    # correct interval #1 to swallow interval #2 -> overlap -> 409, rollback
    r = client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id=1",
        json={"start_s": 50, "end_s": 350, "close_action": "resume"})
    assert r.status_code == 409
    d = _series(client, aid)
    # nothing changed: both intervals remain current, v1 boundaries intact
    ivs = sorted(d["interruptions"]["intervals"], key=lambda x: x["interval_id"])
    assert ivs[0]["start_s"] == 100 and ivs[0]["end_s"] == 200
    assert ivs[1]["start_s"] == 300 and ivs[1]["end_s"] == 400
    assert d["interruptions"]["computable"] is True


def test_key_event_anchor_inside_interval_nulls_active_dtr_keeps_wall(client):
    aid = _seed(client)
    # pin a clean anchor set first
    for ev in [
        {"event_type": "turning_point", "t_s": 60, "source": "manual"},
        {"event_type": "first_crack_start", "t_s": 480, "source": "manual"},
        {"event_type": "drop", "t_s": 600, "source": "manual"},
    ]:
        client.post(f"/api/batches/{aid}/events", json=ev)
    # an interruption covering the first-crack anchor (470..490)
    _start(client, aid, 470.0)
    _resume(client, aid, 490.0)
    m = _series(client, aid)["metrics"]
    assert m["development_s"] == 120 and m["development_ratio"] == 0.2
    assert m["development_active_s"] is None
    assert m["development_ratio_active"] is None
    assert m["active_metrics_status"] == "partial"
    assert m["active_blockers"], "must explain why instead of guessing"


def test_two_overlapping_directly_inserted_conflicts_surface(client):
    """Even when overlap reaches the ledger via a path the live FSM would
    normally block (e.g. imported history), analysis flags it and refuses the
    active DTR rather than fabricating one."""
    aid = _seed(client)
    _start(client, aid, 100.0); _resume(client, aid, 250.0)
    _start(client, aid, 200.0); _resume(client, aid, 300.0)
    d = _series(client, aid)
    assert d["interruptions"]["computable"] is False
    assert any(c["kind"] == "overlap" for c in d["interruptions"]["conflicts"])
    assert d["metrics"]["development_ratio_active"] is None
    assert d["metrics"]["active_metrics_status"] == "conflict"
    # wall DTR still shown — only the active basis is uncomputable
    assert d["metrics"]["development_ratio"] is not None


# ---------------------------------------------------------------- 5

def test_export_and_recompute_agree_on_ledger_and_both_bases(client):
    aid = _seed(client)
    _start(client, aid, 200.0); _resume(client, aid, 260.0)

    ex = client.get(f"/api/batches/{aid}/export?window_s=30&display_smooth_s=12").json()
    assert ex["export_version"] == 2
    assert "interrupt_records" not in ex  # records live under interruptions
    rc = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]
        ],
        "events": ex["events"],
        "interrupt_records": ex["interruptions"]["records"],
        "params": ex["params"],
    }).json()

    # boundaries
    exiv = ex["interruptions"]["intervals"][0]
    rciv = rc["interruptions"]["intervals"][0]
    assert (rciv["start_s"], rciv["end_s"], rciv["duration_s"]) == \
           (exiv["start_s"], exiv["end_s"], exiv["duration_s"])
    # results on both bases
    for key in [
        "drying_s", "maillard_s", "development_s", "total_s", "development_ratio",
        "drying_active_s", "maillard_active_s", "development_active_s",
        "total_active_s", "development_ratio_active",
    ]:
        assert rc["metrics"][key] == ex["metrics"][key], key
    assert rc["elapsed"]["active_elapsed_s"] == ex["elapsed"]["active_elapsed_s"]
    assert rc["time_basis_declaration"]["active_suffix"] == "_active_s"


def test_recompute_replays_historical_version_by_explicit_ids(client):
    aid = _seed(client)
    _start(client, aid, 200.0); _resume(client, aid, 260.0)
    client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id=1",
        json={"start_s": 180, "end_s": 300, "close_action": "resume",
              "source": "post_hoc", "created_by": "supervisor"})
    ex = client.get(f"/api/batches/{aid}/export").json()
    records = ex["interruptions"]["records"]
    v1_ids = [r["id"] for r in records if r["version"] == 1]

    # default recompute -> current version (120 s)
    cur = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]],
        "events": ex["events"], "interrupt_records": records, "params": ex["params"],
    }).json()
    assert cur["interruptions"]["intervals"][0]["duration_s"] == 120

    # explicit historical replay -> old version still reproduces (60 s)
    old = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]],
        "events": ex["events"], "interrupt_records": records,
        "params": ex["params"], "ledger_records_policy": v1_ids,
    }).json()
    assert old["interruptions"]["intervals"][0]["duration_s"] == 60


def test_refresh_is_idempotent_and_raw_unaffected_by_ledger(client):
    aid = _seed(client)
    _start(client, aid, 150.0); _resume(client, aid, 240.0)
    d1 = _series(client, aid)
    d2 = _series(client, aid)  # refresh
    assert d1["interruptions"] == d2["interruptions"]
    assert d1["metrics"] == d2["metrics"]
    assert d1["batch"]["status"] == d2["batch"]["status"] == "resumed"
    # missing-segment audit is unaffected by the ledger (probe loss != outage)
    assert d1["series"]["missing_segments"] == d2["series"]["missing_segments"]
