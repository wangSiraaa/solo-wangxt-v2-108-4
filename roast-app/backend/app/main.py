"""FastAPI application: batch curves, sourced events, comparison, export,
and the auditable interruption ledger."""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import synth
from .analysis import (
    RoRConfig,
    build_interruptions,
    build_series,
    current_events,
    elapsed_summary,
    phase_metrics,
)
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .models import (
    ACTION_RESUME,
    ACTION_START,
    ACTION_TERMINATE,
    BATCH_ENDED,
    BATCH_INTERRUPTED,
    BATCH_IN_PROGRESS,
    BATCH_RESUMED,
    Batch,
    Event,
    InterruptRecord,
    Sample,
    engine,
    init_db,
)
from .schemas import (
    BatchMeta,
    EventIn,
    EventOut,
    InterruptCorrectIn,
    InterruptIn,
    InterruptRecordOut,
)

app = FastAPI(title="Coffee Roast Batch Explorer", version="2.0.0")
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


def _ledger_as_dicts(batch: Batch, *, include_history: bool) -> list[dict]:
    rows = []
    for r in batch.interrupt_records:
        if not include_history and r.superseded:
            continue
        rows.append(
            {
                "id": r.id,
                "batch_id": r.batch_id,
                "action": r.action,
                "interval_id": r.interval_id,
                "version": r.version,
                "t_s": r.t_s,
                "reason": r.reason,
                "note": r.note,
                "source": r.source,
                "created_by": r.created_by,
                "superseded": r.superseded,
                "superseded_by_id": r.superseded_by_id,
                "created_at": r.created_at.isoformat(),
            }
        )
    return rows


def _next_interval_id(session: Session, batch_id: int) -> int:
    mx = session.scalar(
        select(InterruptRecord.interval_id)
        .where(InterruptRecord.batch_id == batch_id)
        .order_by(InterruptRecord.interval_id.desc())
        .limit(1)
    )
    return 1 if mx is None else int(mx) + 1


def _open_interval(
    session: Session, batch_id: int
) -> InterruptRecord | None:
    """The current open interruption (a non-superseded start without a
    non-superseded close), or None."""
    starts = list(session.scalars(
        select(InterruptRecord).where(
            InterruptRecord.batch_id == batch_id,
            InterruptRecord.action == ACTION_START,
            InterruptRecord.superseded.is_(False),
        )
    ))
    closes = {
        r.interval_id
        for r in session.scalars(
            select(InterruptRecord).where(
                InterruptRecord.batch_id == batch_id,
                InterruptRecord.action.in_((ACTION_RESUME, ACTION_TERMINATE)),
                InterruptRecord.superseded.is_(False),
                InterruptRecord.interval_id.is_not(None),
            )
        )
    }
    open_starts = [s for s in starts if s.interval_id not in closes]
    if not open_starts:
        return None
    return sorted(open_starts, key=lambda r: r.id)[-1]


def _ledger_view(batch: Batch, *, include_history: bool, data_end_s: float | None) -> dict:
    records = _ledger_as_dicts(batch, include_history=True)  # validate all versions
    view = build_interruptions(records, data_end_s=data_end_s)
    if not include_history:
        # current rows only are what the operator normally sees
        view["records"] = [r for r in records if not r["superseded"]]
    else:
        view["records"] = records
    return view


def _series_payload(
    batch: Batch,
    *,
    window_s: float,
    display_smooth_s: float,
    max_gap_fill_s: float,
    include_history: bool,
) -> dict[str, Any]:
    sample_dicts = _samples_as_dicts(batch)
    data_end_s = max((s["t_s"] for s in sample_dicts), default=None)
    series = build_series(
        sample_dicts,
        ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
        max_gap_fill_s=max_gap_fill_s,
    )
    events = _events_as_dicts(batch, include_history=include_history)
    ledger = _ledger_view(batch, include_history=include_history, data_end_s=data_end_s)
    metrics = phase_metrics(events, ledger, data_end_s=data_end_s)
    elapsed = elapsed_summary(events, ledger, data_end_s if data_end_s is not None else 0.0)
    return {
        "batch": BatchMeta.model_validate(batch).model_dump(mode="json"),
        "series": series,
        "events": events,
        "interruptions": ledger,
        "elapsed": elapsed,
        "metrics": metrics,
        "params": {
            "ror_window_s": window_s,
            "ror_display_smooth_s": display_smooth_s,
            "max_gap_fill_s": max_gap_fill_s,
            "raw_is_immutable": True,
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
                status=BATCH_IN_PROGRESS,
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
# interruption ledger (append-only, versioned, state-machine enforced)
# ---------------------------------------------------------------------------

def _recompute_batch_status(session: Session, batch: Batch, data_end_s: float | None) -> str:
    """Set batch.status from the ledger itself.  Called after every mutation so
    the stored state can never drift from the records."""
    records = [
        {
            "id": r.id,
            "action": r.action,
            "interval_id": r.interval_id,
            "version": r.version,
            "t_s": r.t_s,
            "reason": r.reason,
            "note": r.note,
            "source": r.source,
            "superseded": r.superseded,
        }
        for r in batch.interrupt_records
    ]
    view = build_interruptions(records, data_end_s=data_end_s)
    if view["ledger_status"] == "ended":
        batch.status = BATCH_ENDED
    elif view["ledger_status"] == "interrupted":
        batch.status = BATCH_INTERRUPTED
    elif view["ledger_status"] == "resumed":
        batch.status = BATCH_RESUMED
    else:
        batch.status = BATCH_IN_PROGRESS
    return view["ledger_status"]


@app.post(
    "/api/batches/{batch_id}/interruptions",
    response_model=list[InterruptRecordOut],
)
def add_interruption(batch_id: int, body: InterruptIn) -> list[InterruptRecord]:
    """Record a live ledger action: start / resume / terminate.

    Server-side lifecycle guard (illegal moves are 409, never silently fixed):
      * resume/terminate-interval require an open interval — a resume with no
        open interruption is rejected outright;
      * a second start while one is open is rejected;
      * no ledger action is accepted on an already-ended batch;
      * terminate when no interruption is open just ends the batch.
    """
    if body.action not in (ACTION_START, ACTION_RESUME, ACTION_TERMINATE):
        raise HTTPException(422, "action must be start | resume | terminate")

    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        if b.status == BATCH_ENDED:
            raise HTTPException(409, "批次已确认终止，不能再记录中断动作")

        open_row = _open_interval(s, batch_id)

        if body.action == ACTION_START:
            if open_row is not None:
                raise HTTPException(
                    409,
                    f"已有未关闭的中断区间 #{open_row.interval_id}，"
                    "重复开始被拒绝（请先记录恢复或终止）",
                )
            iid = _next_interval_id(s, batch_id)
            row = InterruptRecord(
                batch_id=batch_id,
                action=ACTION_START,
                interval_id=iid,
                version=1,
                t_s=body.t_s,
                reason=body.reason,
                note=body.note,
                source=body.source,
                created_by=body.created_by,
            )
            s.add(row)

        elif body.action == ACTION_RESUME:
            if open_row is None:
                raise HTTPException(
                    409,
                    "没有处于打开状态的中断区间：恢复请求被拒绝（不猜测归属）",
                )
            if body.t_s < open_row.t_s:
                raise HTTPException(422, "恢复时刻不能早于中断开始时刻（不猜测）")
            row = InterruptRecord(
                batch_id=batch_id,
                action=ACTION_RESUME,
                interval_id=open_row.interval_id,
                version=open_row.version,
                t_s=body.t_s,
                reason=body.reason,
                note=body.note,
                source=body.source,
                created_by=body.created_by,
            )
            s.add(row)

        else:  # terminate
            row = InterruptRecord(
                batch_id=batch_id,
                action=ACTION_TERMINATE,
                interval_id=None if open_row is None else open_row.interval_id,
                version=1 if open_row is None else open_row.version,
                t_s=body.t_s,
                reason=body.reason,
                note=body.note,
                source=body.source,
                created_by=body.created_by,
            )
            s.add(row)

        s.flush()
        data_end = max((sp.t_s for sp in b.samples), default=None)
        _recompute_batch_status(s, b, data_end)
        s.commit()
        return list(
            s.scalars(
                select(InterruptRecord)
                .where(
                    InterruptRecord.batch_id == batch_id,
                    InterruptRecord.superseded.is_(False),
                )
                .order_by(InterruptRecord.t_s, InterruptRecord.id)
            )
        )


@app.get(
    "/api/batches/{batch_id}/interruptions",
    response_model=list[InterruptRecordOut],
)
def list_interruptions(
    batch_id: int, include_history: bool = Query(False)
) -> list[InterruptRecord]:
    with Session(engine) as s:
        _get_batch(s, batch_id)
        q = select(InterruptRecord).where(InterruptRecord.batch_id == batch_id)
        if not include_history:
            q = q.where(InterruptRecord.superseded.is_(False))
        return list(s.scalars(q.order_by(InterruptRecord.t_s, InterruptRecord.id)))


@app.post(
    "/api/batches/{batch_id}/interruptions/correct",
    response_model=list[InterruptRecordOut],
)
def correct_interruption(
    batch_id: int, body: InterruptCorrectIn, interval_id: int = Query(..., ge=1)
) -> list[InterruptRecord]:
    """Backfill a corrected interval as a NEW VERSION.

    The old version's rows are kept and marked superseded (with
    ``superseded_by_id`` on the new start); metrics shown now use the new
    version, while the old interval — and everything computed from it — remain
    in history/export for audit.  Nothing about the correction rewrites samples
    or events.
    """
    if body.close_action not in (ACTION_RESUME, ACTION_TERMINATE):
        raise HTTPException(422, "close_action must be resume | terminate")
    if body.end_s <= body.start_s:
        raise HTTPException(422, "end_s must be strictly greater than start_s")

    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        old_rows = list(s.scalars(
            select(InterruptRecord).where(
                InterruptRecord.batch_id == batch_id,
                InterruptRecord.interval_id == interval_id,
                InterruptRecord.superseded.is_(False),
            )
        ))
        if not old_rows:
            raise HTTPException(404, f"current interval #{interval_id} not found")
        if not any(r.action == ACTION_START for r in old_rows):
            raise HTTPException(409, f"interval #{interval_id} has no start row")

        max_version = max(int(r.version) for r in old_rows)

        # Reject a correction that structurally overlaps a *different* interval;
        # anchor-over-key-event is never guessed away either — it is surfaced as
        # an anchor conflict by the analysis layer, not fabricated around.
        other_starts = list(s.scalars(
            select(InterruptRecord).where(
                InterruptRecord.batch_id == batch_id,
                InterruptRecord.action == ACTION_START,
                InterruptRecord.superseded.is_(False),
                InterruptRecord.interval_id != interval_id,
            )
        ))
        other_closes = {
            r.interval_id: r
            for r in s.scalars(
                select(InterruptRecord).where(
                    InterruptRecord.batch_id == batch_id,
                    InterruptRecord.action.in_((ACTION_RESUME, ACTION_TERMINATE)),
                    InterruptRecord.superseded.is_(False),
                    InterruptRecord.interval_id != interval_id,
                )
            )
        }
        for os_ in other_starts:
            oc = other_closes.get(os_.interval_id)
            if oc is None:
                continue  # an open other interval: overlap check after insert
            o_lo, o_hi = float(os_.t_s), float(oc.t_s)
            if body.start_s < o_hi and o_lo < body.end_s:
                raise HTTPException(
                    409,
                    f"修正后的区间与区间 #{os_.interval_id} "
                    f"[{o_lo}, {o_hi}) 重叠：拒绝写入冲突账本",
                )

        new_start = InterruptRecord(
            batch_id=batch_id,
            action=ACTION_START,
            interval_id=interval_id,
            version=max_version + 1,
            t_s=body.start_s,
            reason=body.reason,
            note=body.note,
            source=body.source,
            created_by=body.created_by,
        )
        s.add(new_start)
        s.flush()
        new_end = InterruptRecord(
            batch_id=batch_id,
            action=body.close_action,
            interval_id=interval_id,
            version=max_version + 1,
            t_s=body.end_s,
            reason=body.reason,
            note=body.note,
            source=body.source,
            created_by=body.created_by,
        )
        s.add(new_end)
        for old in old_rows:
            old.superseded = True
            old.superseded_by_id = new_start.id
        s.flush()

        data_end = max((sp.t_s for sp in b.samples), default=None)
        view = build_interruptions(
            [
                {
                    "id": r.id, "action": r.action, "interval_id": r.interval_id,
                    "version": r.version, "t_s": r.t_s, "reason": r.reason,
                    "note": r.note, "source": r.source, "superseded": r.superseded,
                }
                for r in b.interrupt_records
            ],
            data_end_s=data_end,
        )
        if not view["computable"]:
            s.rollback()
            raise HTTPException(
                409,
                "修正后的账本存在冲突（如与其他区间重叠）：已拒绝写入；"
                f"检测：{[c['kind'] for c in view['conflicts']]}",
            )
        _recompute_batch_status(s, b, data_end)
        s.commit()
        return list(
            s.scalars(
                select(InterruptRecord)
                .where(
                    InterruptRecord.batch_id == batch_id,
                    InterruptRecord.superseded.is_(False),
                )
                .order_by(InterruptRecord.t_s, InterruptRecord.id)
            )
        )


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
                "中断色带扣除的是加热中断时间，双批次若中断不同，墙钟对齐下"
                "活动时长口径仍在各自指标表内分别给出。"
            ),
        }
        return payload


@app.get("/api/batches/{batch_id}/export")
def export_batch(batch_id: int, window_s: float = 30.0, display_smooth_s: float = 12.0) -> dict[str, Any]:
    """Self-contained export: raw samples, sourced events, the full
    interruption ledger (all versions), parameters, and BOTH time bases of the
    phase metrics.  Everything can be reproduced from raw + events + ledger +
    the stated window (see /api/recompute)."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        payload = _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=MAX_GAP_FILL_S,
            include_history=True,
        )
        payload["export_version"] = 2
        payload["reproducibility"] = {
            "raw_samples_are_source_of_truth": True,
            "metrics_depend_on": [
                "raw_samples",
                "current(non-superseded) events",
                "current(non-superseded) interruption ledger",
                "ror_window_s",
            ],
            "pipeline": (
                "numpy centred least-squares RoR; linear gap fill flagged; "
                "active durations subtract the union of current closed "
                "interruption intervals; conflicts/nulls are exported, never fixed"
            ),
            "ledger_versions": (
                "superseded interrupt records are exported verbatim; current "
                "metrics use version=latest rows only"
            ),
            "batch_status": b.status,
        }
        return payload


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics from an export-style payload.

    Used to verify an export reproduces every stage metric without touching
    the database.  Body:

      {"samples": [...], "events": [...],
       "interrupt_records": [...], "params": {...}}

    By default only non-superseded ledger rows participate (the *current*
    view).  Pass ``ledger_records_policy: "all"`` to additionally see the raw
    validation over every exported row, or a list of record ids to replay a
    historical version explicitly.
    """
    try:
        samples = payload["samples"]
        events = payload.get("events", [])
        records = payload.get("interrupt_records", [])
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
    )
    data_end_s = max((float(sp["t_s"]) for sp in samples), default=None)

    policy = payload.get("ledger_records_policy", "current")
    if policy == "current":
        used_records = [r for r in records if not r.get("superseded", False)]
    elif policy == "all":
        used_records = records
    elif isinstance(policy, list):
        wanted = set(policy)
        # An auditor explicitly replaying historical version rows: honor the
        # selection even though those rows carry superseded=true today.
        used_records = [
            {**r, "superseded": False}
            for r in records
            if r.get("id") in wanted
        ]
    else:
        raise HTTPException(422, "ledger_records_policy must be current | all | [ids]")

    ledger = build_interruptions(used_records, data_end_s=data_end_s)
    metrics = phase_metrics(events, ledger, data_end_s=data_end_s)
    elapsed = elapsed_summary(events, ledger, data_end_s if data_end_s is not None else 0.0)
    return {
        "series": series,
        "metrics": metrics,
        "current_events": current_events(events),
        "interruptions": ledger,
        "elapsed": elapsed,
        "time_basis_declaration": {
            "wall_suffix": "_s",
            "active_suffix": "_active_s",
            "records_policy": policy,
        },
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}
