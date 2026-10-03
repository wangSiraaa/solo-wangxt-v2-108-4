"""FastAPI application: batch curves, sourced events, interruption ledger,
comparison, export."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import synth
from .analysis import (
    RoRConfig,
    build_series,
    current_events,
    current_interruption_intervals,
    interruption_versions,
    phase_metrics,
)
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .models import (
    BATCH_STATES,
    Batch,
    Event,
    InterruptionRecord,
    Sample,
    engine,
    init_db,
)
from .schemas import (
    BatchMeta,
    EventIn,
    EventOut,
    InterruptionCorrectIn,
    InterruptionIn,
    InterruptionRecordOut,
)

app = FastAPI(title="Coffee Roast Batch Explorer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_batch(session: Session, batch_id: int) -> Batch:
    b = session.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return b


def _samples_as_dicts(batch: Batch) -> list[dict]:
    return [
        {
            "t_s": s.t_s,
            "bean_temp_c": s.bean_temp_c,
            "env_temp_c": s.env_temp_c,
        }
        for s in batch.samples
    ]


def _events_as_dicts(batch: Batch, *, include_history: bool) -> list[dict]:
    rows = []
    for e in batch.events:
        if not include_history and e.superseded:
            continue
        rows.append(
            {
                "id": e.id,
                "batch_id": e.batch_id,
                "event_type": e.event_type,
                "t_s": e.t_s,
                "label": e.label,
                "source": e.source,
                "created_by": e.created_by,
                "value_num": e.value_num,
                "note": e.note,
                "superseded": e.superseded,
                "superseded_by_id": e.superseded_by_id,
                "created_at": e.created_at.isoformat(),
            }
        )
    return rows


def _interruptions_as_dicts(
    batch: Batch, *, include_history: bool
) -> list[dict]:
    rows = []
    for r in batch.interruptions:
        if not include_history and r.superseded:
            continue
        rows.append(_interruption_row_dict(r))
    return rows


def _interruption_row_dict(r: InterruptionRecord) -> dict:
    return {
        "id": r.id,
        "batch_id": r.batch_id,
        "interval_id": r.interval_id,
        "version": r.version,
        "action": r.action,
        "t_s": r.t_s,
        "reason": r.reason,
        "source": r.source,
        "created_by": r.created_by,
        "note": r.note,
        "superseded": r.superseded,
        "superseded_by_id": r.superseded_by_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


def _all_interruption_dicts(batch: Batch) -> list[dict]:
    """Full ledger including superseded rows — needed to reproduce any
    historical version and for the self-contained export."""
    return [_interruption_row_dict(r) for r in batch.interruptions]


def _ledger_summary(batch: Batch, *, include_history: bool) -> dict:
    """Current interpretation plus the versioned audit trail."""
    full = _all_interruption_dicts(batch)
    current = current_interruption_intervals(full)
    summary = {
        "status": batch.status,
        "current": {
            "intervals": current["intervals"],
            "open": current["open"],
            "computable": current["computable"],
            "conflicts": current["conflicts"],
        },
        "records": _interruptions_as_dicts(batch, include_history=include_history),
    }
    if include_history:
        summary["versions"] = interruption_versions(full)
    return summary


def _active_horizon_s(batch: Batch) -> float | None:
    ts = [s.t_s for s in batch.samples]
    return max(ts) if ts else None


def _series_payload(
    batch: Batch,
    *,
    window_s: float,
    display_smooth_s: float,
    max_gap_fill_s: float,
    include_history: bool,
) -> dict[str, Any]:
    sample_dicts = _samples_as_dicts(batch)
    interruptions = _all_interruption_dicts(batch)
    events = _events_as_dicts(batch, include_history=include_history)
    series = build_series(
        sample_dicts,
        ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
        max_gap_fill_s=max_gap_fill_s,
        interruption_records=interruptions,
    )
    return {
        "batch": BatchMeta.model_validate(batch).model_dump(mode="json"),
        "series": series,
        "events": events,
        "interruptions": _ledger_summary(batch, include_history=include_history),
        "metrics": phase_metrics(
            events,
            interruptions,
            active_horizon_s=_active_horizon_s(batch),
        ),
        "params": {
            "ror_window_s": window_s,
            "ror_display_smooth_s": display_smooth_s,
            "max_gap_fill_s": max_gap_fill_s,
            "raw_is_immutable": True,
            "time_basis": "wall_clock_s and *_active_s (closed interruptions excluded)",
        },
    }


# ---------------------------------------------------------------------------
# batches / seeding
# ---------------------------------------------------------------------------

@app.get("/api/batches", response_model=list[BatchMeta])
def list_batches() -> list[Batch]:
    with Session(engine) as s:
        return list(s.scalars(select(Batch).order_by(Batch.id)))


@app.post("/api/seed", response_model=list[BatchMeta])
def seed_demo() -> list[Batch]:
    """Load the two synthetic demo batches (noise + dropouts, no machine)."""
    with Session(engine) as s:
        created: list[Batch] = []
        for spec in synth.two_demo_batches():
            existing = s.scalar(select(Batch).where(Batch.name == spec["name"]))
            if existing is not None:
                created.append(existing)
                continue
            b = Batch(
                name=spec["name"],
                roaster=spec["roaster"],
                bean=spec["bean"],
                charge_at=spec["charge_at"],
                charge_temp_c=spec["charge_temp_c"],
                ambient_temp_c=spec["ambient_temp_c"],
                target_drop_temp_c=spec["target_drop_temp_c"],
                note=spec["note"],
            )
            b.samples = [
                Sample(
                    t_s=sp["t_s"],
                    bean_temp_c=sp["bean_temp_c"],
                    env_temp_c=sp["env_temp_c"],
                )
                for sp in spec["samples"]
            ]
            b.events = [Event(**ev) for ev in spec["events"]]
            s.add(b)
            created.append(b)
        s.commit()
        for b in created:
            s.refresh(b)
        return created


@app.get("/api/batches/{batch_id}/series")
def get_series(
    batch_id: int,
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
    include_history: bool = Query(False),
) -> dict[str, Any]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        return _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=max_gap_fill_s,
            include_history=include_history,
        )


# ---------------------------------------------------------------------------
# events: append-only corrections with provenance
# ---------------------------------------------------------------------------

@app.post("/api/batches/{batch_id}/events", response_model=EventOut)
def add_event(batch_id: int, ev: EventIn) -> Event:
    with Session(engine) as s:
        _get_batch(s, batch_id)
        row = Event(batch_id=batch_id, **ev.model_dump())
        s.add(row)
        s.flush()
        # Only one *current* event per type: supersede the previous current one.
        if row.event_type != "damper_change":
            prev = s.scalars(
                select(Event).where(
                    Event.batch_id == batch_id,
                    Event.event_type == row.event_type,
                    Event.superseded.is_(False),
                    Event.id != row.id,
                )
            ).all()
            for p in prev:
                p.superseded = True
                p.superseded_by_id = row.id
        s.commit()
        s.refresh(row)
        return row


@app.get("/api/batches/{batch_id}/events", response_model=list[EventOut])
def list_events(batch_id: int, include_history: bool = Query(False)) -> list[Event]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        q = select(Event).where(Event.batch_id == batch_id)
        if not include_history:
            q = q.where(Event.superseded.is_(False))
        return list(s.scalars(q.order_by(Event.t_s)))


# ---------------------------------------------------------------------------
# interruption ledger: auditable heat-off episodes
#
# The ledger is append-only like the event table, but it ALSO drives the
# batch lifecycle state machine:
#
#   in_progress --start--> interrupted --resume--> resumed --start--> interrupted
#        |                      |                     |
#        +------terminate-------+-----terminate-------+--> ended
#
# A resume with no open interruption, a duplicate resume, and any action on an
# ended batch are rejected — the active roasting time would otherwise be
# double-counted or invented.  Overlapping current intervals are not rejected
# at write time (they may be legitimately reported); they are recorded and the
# active-time metrics refuse to compute until corrected in a NEW version.
# ---------------------------------------------------------------------------

def _open_interruption(
    session: Session, batch_id: int
) -> InterruptionRecord | None:
    """The current (non-superseded) start row whose episode is not closed."""
    starts = list(
        session.scalars(
            select(InterruptionRecord).where(
                InterruptionRecord.batch_id == batch_id,
                InterruptionRecord.action == "start",
                InterruptionRecord.superseded.is_(False),
            )
        )
    )
    for st in starts:
        closed = session.scalar(
            select(InterruptionRecord.id).where(
                InterruptionRecord.batch_id == batch_id,
                InterruptionRecord.interval_id == st.interval_id,
                InterruptionRecord.action.in_(("resume", "terminate")),
                InterruptionRecord.superseded.is_(False),
            )
        )
        if closed is None:
            return st
    return None


def _reconcile_status(session: Session, batch: Batch) -> str:
    """Recompute the status strictly from the current ledger rows and persist
    it.  Keeps the stored column in lock-step with the ledger so the state
    machine cannot drift."""
    full = [
        _interruption_row_dict(r)
        for r in session.scalars(
            select(InterruptionRecord)
            .where(InterruptionRecord.batch_id == batch.id)
            .order_by(InterruptionRecord.id)
        )
    ]
    interp = current_interruption_intervals(full)
    assert interp["batch_status"] in BATCH_STATES
    batch.status = interp["batch_status"]
    return batch.status


@app.get("/api/batches/{batch_id}/interruptions")
def list_interruptions(batch_id: int, include_history: bool = Query(False)) -> dict[str, Any]:
    with Session(engine) as s:
        _get_batch(s, batch_id)
        b = s.get(Batch, batch_id)
        return _ledger_summary(b, include_history=include_history)


@app.post(
    "/api/batches/{batch_id}/interruptions",
    response_model=InterruptionRecordOut,
)
def add_interruption(batch_id: int, body: InterruptionIn) -> InterruptionRecord:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        if b.status == "ended":
            raise HTTPException(409, "批次已结束（terminated），不能再登记中断动作。")

        open_row = _open_interruption(s, batch_id)

        if body.action == "start":
            if open_row is not None:
                raise HTTPException(
                    409,
                    f"已有未关闭的中断（开始于 {open_row.t_s}s）；"
                    "重复开始不会另开区间，请先恢复或终止。",
                )
            row = InterruptionRecord(
                batch_id=batch_id,
                interval_id=str(uuid.uuid4()),
                version=1,
                action="start",
                t_s=body.t_s,
                reason=body.reason,
                source=body.source,
                created_by=body.created_by,
                note=body.note,
            )
            s.add(row)
            b.status = "interrupted"

        elif body.action == "resume":
            if open_row is None:
                # No start to close: never fabricate one.
                raise HTTPException(
                    409,
                    "没有未关闭的中断，恢复请求被拒绝（不能凭空增加活动时长口径）。",
                )
            if body.t_s < open_row.t_s:
                raise HTTPException(
                    422,
                    f"恢复时刻 {body.t_s}s 早于中断开始 {open_row.t_s}s。",
                )
            # open_row exists => no current resume/terminate exists, so a
            # duplicate resume can never reach here (a second POST finds no
            # open row and is rejected above) — active time cannot be counted
            # twice.
            row = InterruptionRecord(
                batch_id=batch_id,
                interval_id=open_row.interval_id,
                version=open_row.version,
                action="resume",
                t_s=body.t_s,
                reason=body.reason,
                source=body.source,
                created_by=body.created_by,
                note=body.note,
            )
            s.add(row)
            b.status = "resumed"

        else:  # terminate
            if open_row is not None:
                if body.t_s < open_row.t_s:
                    raise HTTPException(
                        422,
                        f"终止时刻 {body.t_s}s 早于中断开始 {open_row.t_s}s。",
                    )
                row = InterruptionRecord(
                    batch_id=batch_id,
                    interval_id=open_row.interval_id,
                    version=open_row.version,
                    action="terminate",
                    t_s=body.t_s,
                    reason=body.reason,
                    source=body.source,
                    created_by=body.created_by,
                    note=body.note,
                )
            else:
                # Terminating with everything closed: a batch-level end marker
                # without an interval (interval_id NULL).
                row = InterruptionRecord(
                    batch_id=batch_id,
                    interval_id=None,
                    version=1,
                    action="terminate",
                    t_s=body.t_s,
                    reason=body.reason,
                    source=body.source,
                    created_by=body.created_by,
                    note=body.note,
                )
            s.add(row)
            b.status = "ended"
            b.ended_at = datetime.utcnow()

        s.commit()
        s.refresh(row)
        return row


@app.post(
    "/api/batches/{batch_id}/interruptions/correct",
    response_model=list[InterruptionRecordOut],
)
def correct_interruption(
    batch_id: int,
    body: InterruptionCorrectIn,
    interval_id: str = Query(..., description="要修正的逻辑中断 episode id"),
) -> list[InterruptionRecord]:
    """Backdated correction.  The old rows are kept (superseded) and a NEW
    version is appended; current metrics are recomputed from the new version,
    historical metrics remain auditable via ``versions``."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        old_rows = list(
            s.scalars(
                select(InterruptionRecord).where(
                    InterruptionRecord.batch_id == batch_id,
                    InterruptionRecord.interval_id == interval_id,
                ).order_by(InterruptionRecord.id)
            )
        )
        if not old_rows:
            raise HTTPException(404, f"interval {interval_id} not found")
        current_rows = [r for r in old_rows if not r.superseded]
        if not current_rows:
            raise HTTPException(409, "该中断区间没有当前版本可修正。")
        new_version = max(r.version for r in old_rows) + 1

        # Mark every current row of this episode superseded by the new start.
        new_start = InterruptionRecord(
            batch_id=batch_id,
            interval_id=interval_id,
            version=new_version,
            action="start",
            t_s=body.start_s,
            reason=body.reason,
            source=body.source,
            created_by=body.created_by,
            note=body.note or f"后补修正（v{new_version}）：旧区间保留可审计",
        )
        s.add(new_start)
        s.flush()
        for r in current_rows:
            r.superseded = True
            r.superseded_by_id = new_start.id
        created = [new_start]
        if body.end_s is not None:
            end_row = InterruptionRecord(
                batch_id=batch_id,
                interval_id=interval_id,
                version=new_version,
                action=body.end_action,
                t_s=body.end_s,
                reason=body.end_reason,
                source=body.source,
                created_by=body.created_by,
                note=body.note or f"后补修正（v{new_version}）",
            )
            s.add(end_row)
            created.append(end_row)

        # Lifecycle follows the corrected ledger.
        new_status = _reconcile_status(s, b)
        if new_status == "ended" and b.ended_at is None:
            b.ended_at = datetime.utcnow()
        elif new_status != "ended":
            b.ended_at = None
        s.commit()
        for r in created:
            s.refresh(r)
        return created


# ---------------------------------------------------------------------------
# comparison (no causal claims) + export / recompute
# ---------------------------------------------------------------------------

@app.get("/api/compare")
def compare(
    a: int = Query(..., description="first batch id"),
    b: int = Query(..., description="second batch id"),
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
) -> dict[str, Any]:
    """Overlay two batches on charge-relative time. Damper changes are shown
    as marks so the operator can eyeball before/after shape; the API attaches
    an explicit non-causal note."""
    with Session(engine) as s:
        ba, bb = _get_batch(s, a), _get_batch(s, b)
        payload = {
            "batches": [
                _series_payload(
                    ba,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
                _series_payload(
                    bb,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
            ],
            "interpretation": (
                "曲线按开火/下豆时刻对齐叠加。风门变化以标记线显示，"
                "前后形态仅供观察对比，不构成因果结论（无对照、无重复、无统计检验）。"
            ),
        }
        return payload


@app.get("/api/batches/{batch_id}/export")
def export_batch(batch_id: int, window_s: float = 30.0, display_smooth_s: float = 12.0) -> dict[str, Any]:
    """Self-contained export: raw samples, sourced events, the FULL
    interruption ledger (all versions, including superseded rows), parameters,
    and the derived phase metrics on both time bases.  Every metric can be
    reproduced from raw + events + ledger + the stated window (see
    /api/recompute)."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        payload = _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=MAX_GAP_FILL_S,
            include_history=True,
        )
        full_ledger = _all_interruption_dicts(b)
        horizon = _active_horizon_s(b)
        # Historical metrics for every ledger version: what the operator would
        # have seen under each prior version, kept for audit after corrections.
        history = []
        for v in interruption_versions(full_ledger):
            # as_of just after the latest row written for that version, so the
            # version's interval (open or closed) is reproduced as it existed
            # immediately after that correction was recorded.
            if not v["as_of"]:
                continue
            as_of_iso = v["as_of"]
            history.append(
                {
                    "interval_id": v["interval_id"],
                    "version": v["version"],
                    "as_of": as_of_iso,
                    "superseded": v["superseded"],
                    "interval": {
                        "start_s": v["start_s"],
                        "end_s": v["end_s"],
                        "open": v["open"],
                    },
                    "metrics_as_of_version": phase_metrics(
                        _events_as_dicts(b, include_history=True),
                        full_ledger,
                        active_horizon_s=horizon,
                        as_of=as_of_iso,
                    ),
                }
            )
        payload["export_version"] = 2
        payload["reproducibility"] = {
            "raw_samples_are_source_of_truth": True,
            "raw_and_gap_handling_never_modified_by_ledger": True,
            "metrics_depend_on": [
                "raw_samples",
                "current(non-superseded) events",
                "current(non-superseded) interruption ledger",
                "ror_window_s",
            ],
            "time_bases": {
                "wall_clock": "plain t1 - t0, ledger-independent",
                "active_roasting": (
                    "wall-clock minus union of CLOSED interruption intervals; "
                    "null on conflict/open overlap/anchor-inside-band"
                ),
            },
            "pipeline": "numpy centred least-squares RoR; linear gap fill flagged",
        }
        payload["interruption_ledger_full"] = full_ledger
        payload["interruption_metric_history"] = history
        return payload


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics from an export-style payload.

    Used to verify an export reproduces every stage metric without touching
    the database.  Body: {"samples": [...], "events": [...],
    "interruptions": [...all ledger rows...], "params": {...}}.
    """
    try:
        samples = payload["samples"]
        events = payload.get("events", [])
        interruptions = payload.get("interruptions", [])
        params = payload.get("params", {})
    except KeyError as exc:
        raise HTTPException(422, f"missing field: {exc}")
    cfg = RoRConfig(
        window_s=float(params.get("ror_window_s", 30.0)),
        display_smooth_s=float(params.get("ror_display_smooth_s", 12.0)),
    )
    series = build_series(
        samples,
        ror_cfg=cfg,
        max_gap_fill_s=float(params.get("max_gap_fill_s", MAX_GAP_FILL_S)),
        interruption_records=interruptions,
    )
    horizon = max((float(s["t_s"]) for s in samples), default=None)
    return {
        "series": series,
        "metrics": phase_metrics(
            events, interruptions, active_horizon_s=horizon
        ),
        "current_events": current_events(events),
        "interruption_summary": current_interruption_intervals(interruptions),
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}
