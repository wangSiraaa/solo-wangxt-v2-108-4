"""End-to-end API tests for the auditable interruption ledger.

Covers the five acceptance items:
  ① legal interruption + resume: wall & active time, raw curve intact;
  ② duplicate resume not double counted; resume with no open interval rejected;
  ③ backdated correction = new version, old interval/metrics auditable, current
    metrics recomputed;
  ④ overlapping / anchor-covering intervals flagged conflict, no fake DTR;
  ⑤ refresh / export / independent recompute agree on boundaries, basis, result.
"""
import pytest

from app import models


@pytest.fixture(autouse=True)
def _fresh_ledger_db():
    # The session client shares one SQLite file; reset per test so ledger rows
    # from one acceptance scenario can never leak into another.
    models.Base.metadata.drop_all(models.engine)
    models.Base.metadata.create_all(models.engine)
    yield


def _seed(client):
    r = client.post("/api/seed")
    assert r.status_code == 200
    return r.json()[0]["id"]


def _clean_anchors(client, aid, drop_t=600.0):
    """Manual anchors at clean round times so active math is exact."""
    for ev in [
        {"event_type": "turning_point", "t_s": 60, "source": "manual"},
        {"event_type": "first_crack_start", "t_s": 480, "source": "manual"},
        {"event_type": "first_crack_end", "t_s": 535, "source": "manual"},
        {"event_type": "drop", "t_s": drop_t, "source": "manual"},
    ]:
        assert client.post(f"/api/batches/{aid}/events", json=ev).status_code == 200


# ---------------------------------------------------------------------------
# ① legal start -> resume: both bases, raw curve intact
# ---------------------------------------------------------------------------

def test_legal_interruption_resume_wall_and_active(client):
    aid = _seed(client)
    _clean_anchors(client, aid)

    r1 = client.post(f"/api/batches/{aid}/interruptions", json={
        "action": "start", "t_s": 300, "reason": "power_cut",
        "created_by": "charge-hand"})
    assert r1.status_code == 200
    assert client.get(f"/api/batches/{aid}/series").json()["batch"]["status"] == "interrupted"

    r2 = client.post(f"/api/batches/{aid}/interruptions", json={
        "action": "resume", "t_s": 360, "reason": "restored"})
    assert r2.status_code == 200
    assert r2.json()["interval_id"] == r1.json()["interval_id"]

    d = client.get(f"/api/batches/{aid}/series").json()
    assert d["batch"]["status"] == "resumed"
    m = d["metrics"]
    assert m["total_s"] == 600                 # wall clock
    assert m["total_active_s"] == 540         # 60 s heat-off removed
    assert m["maillard_s"] == 420
    assert m["maillard_active_s"] == 360
    assert m["development_active_s"] == 120
    assert abs(m["development_ratio_active"] - 120 / 540) < 1e-3
    assert m["active_time_computable"] is True

    # visible band on the curve
    bands = d["series"]["interruption_bands"]
    assert len(bands) == 1 and bands[0]["start_s"] == 300 and bands[0]["end_s"] == 360

    # raw curve still complete and unchanged: NULLs stay NULL, no rows dropped
    raw = d["series"]["raw_points"]
    assert any(p["bean_temp_c"] is None for p in raw)  # probe dropouts preserved
    gap_segments = d["series"]["missing_segments"]
    assert any(g["channel"] == "bean" for g in gap_segments)


def test_status_transitions_are_enforced(client):
    aid = _seed(client)
    get = lambda: client.get(f"/api/batches/{aid}/series").json()["batch"]["status"]
    assert get() == "in_progress"
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 100})
    assert get() == "interrupted"
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "resume", "t_s": 200})
    assert get() == "resumed"
    # second episode is legal
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 300})
    assert get() == "interrupted"
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "terminate", "t_s": 590})
    assert get() == "ended"
    # any further ledger action is refused
    r = client.post(f"/api/batches/{aid}/interruptions",
                    json={"action": "start", "t_s": 595})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# ② duplicate resume / no-start resume
# ---------------------------------------------------------------------------

def test_duplicate_resume_rejected_and_not_double_counted(client):
    aid = _seed(client)
    _clean_anchors(client, aid)
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 300})
    ok = client.post(f"/api/batches/{aid}/interruptions",
                     json={"action": "resume", "t_s": 360})
    assert ok.status_code == 200

    again = client.post(f"/api/batches/{aid}/interruptions",
                        json={"action": "resume", "t_s": 400})
    assert again.status_code == 409

    led = client.get(f"/api/batches/{aid}/interruptions").json()
    resumes = [r for r in led["records"] if r["action"] == "resume"]
    assert len(resumes) == 1  # exactly one resume recorded

    m = client.get(f"/api/batches/{aid}/series").json()["metrics"]
    assert m["total_active_s"] == 540  # 60 s only, not 100 s


def test_resume_without_open_interruption_rejected(client):
    aid = _seed(client)
    r = client.post(f"/api/batches/{aid}/interruptions",
                    json={"action": "resume", "t_s": 100})
    assert r.status_code == 409
    led = client.get(f"/api/batches/{aid}/interruptions").json()
    assert led["records"] == []
    assert led["status"] == "in_progress"


def test_start_while_open_rejected(client):
    aid = _seed(client)
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 100})
    r = client.post(f"/api/batches/{aid}/interruptions",
                    json={"action": "start", "t_s": 120})
    assert r.status_code == 409


def test_resume_before_start_rejected(client):
    aid = _seed(client)
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 200})
    r = client.post(f"/api/batches/{aid}/interruptions",
                    json={"action": "resume", "t_s": 100})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ③ backdated correction: versioning + audit history
# ---------------------------------------------------------------------------

def test_backdated_correction_creates_version_and_keeps_history(client):
    aid = _seed(client)
    _clean_anchors(client, aid)
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 300, "reason": "power_cut"})
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "resume", "t_s": 360, "reason": "restored"})

    before = client.get(f"/api/batches/{aid}/series").json()["metrics"]
    assert before["total_active_s"] == 540

    interval_id = client.get(f"/api/batches/{aid}/interruptions").json()["current"]["intervals"][0]["interval_id"]

    # corrected: real episode was 310 -> 340 (30 s)
    rc = client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id={interval_id}",
        json={"start_s": 310, "end_s": 340, "end_action": "resume",
              "reason": "safety_check(核对)", "created_by": "supervisor"},
    )
    assert rc.status_code == 200
    assert [r["version"] for r in rc.json()] == [2, 2]

    after = client.get(f"/api/batches/{aid}/series").json()["metrics"]
    assert after["total_active_s"] == 570
    assert after["total_interrupted_s"] == 30

    # history view: old v1 rows remain, marked superseded, linked to new start
    led = client.get(f"/api/batches/{aid}/interruptions?include_history=true").json()
    v1 = [r for r in led["records"] if r["version"] == 1]
    v2 = [r for r in led["records"] if r["version"] == 2]
    assert len(v1) == 2 and all(r["superseded"] for r in v1)
    assert len(v2) == 2 and all(not r["superseded"] for r in v2)
    assert all(r["superseded_by_id"] == v2[0]["id"] for r in v1)

    versions = led["versions"]
    by_v = {v["version"]: v for v in versions}
    assert by_v[1]["superseded"] is True
    assert by_v[1]["start_s"] == 300
    assert by_v[1]["end_s"] == 360
    assert by_v[2]["start_s"] == 310

    # v1's reported metrics remain available
    v1_metrics = next(
        h for h in client.get(f"/api/batches/{aid}/export").json()["interruption_metric_history"]
        if h["version"] == 1
    )
    assert v1_metrics["interval"]["start_s"] == 300
    assert v1_metrics["interval"]["end_s"] == 360
    assert v1_metrics["metrics_as_of_version"]["total_active_s"] == 540


def test_correction_unknown_interval_404(client):
    aid = _seed(client)
    r = client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id=does-not-exist",
        json={"start_s": 1, "end_s": 2},
    )
    assert r.status_code == 404


def test_correction_end_before_start_rejected(client):
    aid = _seed(client)
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 300})
    iid = client.get(f"/api/batches/{aid}/interruptions").json()["current"]["intervals"][0]["interval_id"]
    r = client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id={iid}",
        json={"start_s": 300, "end_s": 200},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ④ overlapping / anchor-covering intervals: conflict, no fabricated DTR
# ---------------------------------------------------------------------------

def test_overlapping_intervals_flagged_active_dtr_withheld(client):
    aid = _seed(client)
    _clean_anchors(client, aid)

    def episode(start, end, tag):
        r = client.post(f"/api/batches/{aid}/interruptions",
                        json={"action": "start", "t_s": start, "reason": tag})
        assert r.status_code == 200
        iid = r.json()["interval_id"]
        r2 = client.post(f"/api/batches/{aid}/interruptions",
                         json={"action": "resume", "t_s": end})
        assert r2.status_code == 200
        return iid

    # Episode 1 closed 300-400; episode 2 backdated to overlap 350-450.
    episode(300, 400, "power_cut")
    iid2 = episode(350, 450, "safety_check")
    d = client.get(f"/api/batches/{aid}/series").json()
    codes = [c["code"] for c in d["metrics"]["interruption_basis"]["conflicts"]]
    assert "overlapping_interruptions" in codes
    assert d["metrics"]["development_ratio_active"] is None
    assert d["metrics"]["total_active_s"] is None
    # wall-clock metrics are unaffected and explicit
    assert d["metrics"]["total_s"] == 600
    assert d["metrics"]["development_ratio"] == 0.2


def test_anchor_inside_band_flags_conflict(client):
    aid = _seed(client)
    _clean_anchors(client, aid)
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 470})
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "resume", "t_s": 500})
    m = client.get(f"/api/batches/{aid}/series").json()["metrics"]
    codes = [c["code"] for c in m["interruption_basis"]["conflicts"]]
    assert "phase_anchor_inside_interruption" in codes
    assert m["maillard_active_s"] is None
    assert m["development_active_s"] is None
    assert m["development_ratio_active"] is None
    assert m["drying_active_s"] == 60  # unaffected phase still computes


def test_correction_resolves_conflict(client):
    """Overlap introduced by backdated episodes can be repaired with another
    correction; active metrics then compute again."""
    aid = _seed(client)
    _clean_anchors(client, aid)
    # episode A: 300-400
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 300})
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "resume", "t_s": 400})
    # episode B backdated 350-450 -> overlap
    rb = client.post(f"/api/batches/{aid}/interruptions",
                     json={"action": "start", "t_s": 350})
    iid_b = rb.json()["interval_id"]
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "resume", "t_s": 450})
    m1 = client.get(f"/api/batches/{aid}/series").json()["metrics"]
    assert m1["total_active_s"] is None

    # correct B to 410-450 -> no overlap
    r = client.post(
        f"/api/batches/{aid}/interruptions/correct?interval_id={iid_b}",
        json={"start_s": 410, "end_s": 450, "end_action": "resume"})
    assert r.status_code == 200
    m2 = client.get(f"/api/batches/{aid}/series").json()["metrics"]
    # A: 100 s off (300-400); B: 40 s off (410-450); disjoint -> 140 s total
    assert m2["total_active_s"] == 460
    assert m2["active_time_computable"] is True


# ---------------------------------------------------------------------------
# ⑤ refresh / export / recompute consistency
# ---------------------------------------------------------------------------

def test_export_recompute_agree_on_interruption_basis(client):
    aid = _seed(client)
    _clean_anchors(client, aid)
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 300, "reason": "power_cut"})
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "resume", "t_s": 360})

    # refresh twice: identical boundaries (deterministic)
    d1 = client.get(f"/api/batches/{aid}/series").json()
    d2 = client.get(f"/api/batches/{aid}/series").json()
    assert d1["metrics"] == d2["metrics"]
    assert d1["series"]["interruption_bands"] == d2["series"]["interruption_bands"]

    ex = client.get(f"/api/batches/{aid}/export").json()
    assert ex["export_version"] >= 2
    # full ledger incl. both rows
    assert len(ex["interruption_ledger_full"]) == 2
    rc = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]
        ],
        "events": ex["events"],
        "interruptions": ex["interruption_ledger_full"],
        "params": ex["params"],
    }).json()
    for key in [
        "drying_s", "maillard_s", "development_s", "first_crack_window_s",
        "total_s", "development_ratio",
        "drying_active_s", "maillard_active_s", "development_active_s",
        "first_crack_window_active_s", "total_active_s",
        "development_ratio_active", "total_interrupted_s",
        "active_time_computable",
    ]:
        assert rc["metrics"][key] == ex["metrics"][key], key
    # bands agree
    assert rc["series"]["interruption_bands"] == ex["series"]["interruption_bands"]
    # recompute summary marks ledger computable with no conflicts
    assert rc["interruption_summary"]["computable"] is True


def test_export_recompute_conflict_state_consistent(client):
    aid = _seed(client)
    _clean_anchors(client, aid)
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "start", "t_s": 300})
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "resume", "t_s": 400})
    rb = client.post(f"/api/batches/{aid}/interruptions",
                     json={"action": "start", "t_s": 350})
    client.post(f"/api/batches/{aid}/interruptions",
                json={"action": "resume", "t_s": 450})
    ex = client.get(f"/api/batches/{aid}/export").json()
    rc = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]
        ],
        "events": ex["events"],
        "interruptions": ex["interruption_ledger_full"],
        "params": ex["params"],
    }).json()
    assert rc["metrics"]["development_ratio_active"] is None
    assert rc["metrics"]["development_ratio"] == 0.2
    ex_codes = {c["code"] for c in ex["metrics"]["interruption_basis"]["conflicts"]}
    rc_codes = {c["code"] for c in rc["metrics"]["interruption_basis"]["conflicts"]}
    assert ex_codes == rc_codes == {"overlapping_interruptions"}


def test_terminate_without_open_interval_marks_ended(client):
    aid = _seed(client)
    r = client.post(f"/api/batches/{aid}/interruptions",
                    json={"action": "terminate", "t_s": 600})
    assert r.status_code == 200
    assert r.json()["interval_id"] is None
    assert client.get(f"/api/batches/{aid}/series").json()["batch"]["status"] == "ended"
