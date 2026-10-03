"""NumPy analysis pipeline: rate-of-rise, gap handling, phase metrics.

All temperature transformations live here and are *pure functions of the raw
samples*.  Nothing in this module writes back to the database: smoothing and
interpolation parameters only change derived outputs, never measured data.

Rate-of-rise (RoR) definition
-----------------------------
Sampling is uneven, so a naive ``(T[i+1]-T[i]) / dt`` is meaningless.
Instead, at every observed time ``t`` we fit an ordinary (time-weighted)
least-squares line to the measured bean-temperature points inside a
**centred time window** ``[t - window_s/2, t + window_s/2]`` and take its
slope, converted to °C/min:

    slope = sum w (x-x̄)(y-ȳ) / sum w (x-x̄)²      with x in seconds, w=1

The window is reported alongside every output so consumers always know the
basis of the number.  Points inside a probe-dropout gap are *not* used: only
measured samples count, and a slope is only emitted when at least
``min_points`` measured points spanning at least ``min_span_s`` lie in the
window.  At the series edges the window is truncated (still centred as far as
the data allows); ``edge`` flags those points.

Interpolation
-------------
Missing bean temperatures (probe loss) are bridged with a **linear
interpolation between measured endpoints, marked ``is_interpolated=True``**.
Gaps wider than ``max_gap_fill_s`` are deliberately NOT bridged — the chart
shows a break instead of inventing data.  Interpolated points are never fed
to the RoR regression as if measured.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EVENT_TIME_KEYS = (
    "charge",
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
)


@dataclass(frozen=True)
class RoRConfig:
    window_s: float = 30.0
    min_points: int = 4
    min_span_s: float = 10.0
    # Extra smoothing applied to the *displayed* RoR trace only.
    display_smooth_s: float = 0.0


def _linear_fill(
    t: np.ndarray,
    temp: np.ndarray,
    max_gap_fill_s: float,
    channel: str = "bean",
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Return (filled_temp, is_interpolated_mask, gap_records).

    Only internal gaps no wider than ``max_gap_fill_s`` are filled.  Edge
    missing values and oversized gaps stay NaN.
    """
    filled = temp.astype(float).copy()
    is_interp = np.zeros_like(t, dtype=bool)
    measured = ~np.isnan(temp)
    gaps: list[dict] = []

    idx = np.arange(len(t))
    miss_idx = idx[~measured]
    if len(miss_idx) == 0:
        return filled, is_interp, gaps

    # Group consecutive missing indices into runs.
    starts: list[int] = []
    ends: list[int] = []
    run_start = int(miss_idx[0])
    prev = run_start
    for j in miss_idx[1:]:
        j = int(j)
        if j != prev + 1:
            starts.append(run_start)
            ends.append(prev)
            run_start = j
        prev = j
    starts.append(run_start)
    ends.append(prev)

    for a, b in zip(starts, ends):
        # Width is measured between the measured neighbours — what an
        # interpolation would actually span.
        gap_width = float(t[b + 1] - t[a - 1]) if 0 < a and b < len(t) - 1 else None
        if a == 0 or b == len(t) - 1:
            kind = "edge_unfilled"
        elif gap_width > max_gap_fill_s:
            kind = "wide_unfilled"
        else:
            kind = "interpolated"
            t0, t1 = t[a - 1], t[b + 1]
            y0, y1 = temp[a - 1], temp[b + 1]
            seg_t = t[a : b + 1]
            filled[a : b + 1] = y0 + (y1 - y0) * (seg_t - t0) / (t1 - t0)
            is_interp[a : b + 1] = True
        gaps.append(
            {
                "channel": channel,
                "t_start_s": float(t[a]),
                "t_end_s": float(t[b]),
                "n_missing": int(b - a + 1),
                "span_s": float(t[b] - t[a]),
                "neighbour_span_s": gap_width,
                "status": kind,
            }
        )
    return filled, is_interp, gaps


def rate_of_rise(
    t: np.ndarray,
    temp: np.ndarray,
    cfg: RoRConfig,
) -> dict:
    """Centred moving-window least-squares RoR over measured points only.

    Returns raw per-point RoR (°C/min) plus an optional trailing-mean display
    trace.  Both share the same regression window; the display trace adds an
    independent smoothing pass whose parameter is surfaced in metadata.
    """
    t = np.asarray(t, dtype=float)
    temp = np.asarray(temp, dtype=float)
    n = len(t)
    half = cfg.window_s / 2.0
    ror = np.full(n, np.nan)
    n_used = np.zeros(n, dtype=int)
    spans = np.full(n, np.nan)
    edge = np.zeros(n, dtype=bool)

    measured = ~np.isnan(temp)
    t_m = t[measured]
    y_m = temp[measured]

    for i in range(n):
        if not measured[i]:
            continue
        lo = t[i] - half
        hi = t[i] + half
        inside = (t_m >= lo) & (t_m <= hi)
        xs = t_m[inside]
        ys = y_m[inside]
        span = xs.max() - xs.min() if len(xs) >= 2 else 0.0
        n_used[i] = len(xs)
        spans[i] = span
        if t[i] - half < t_m.min() - 1e-9 or t[i] + half > t_m.max() + 1e-9:
            edge[i] = True
        if len(xs) >= cfg.min_points and span >= cfg.min_span_s:
            x = xs - xs.mean()
            denom = float((x * x).sum())
            if denom > 0:
                ror[i] = 60.0 * float((x * (ys - ys.mean())).sum()) / denom

    display = ror.copy()
    if cfg.display_smooth_s > 0:
        display = _time_weighted_smooth(t, ror, cfg.display_smooth_s, measured)

    return {
        "ror_raw": ror,
        "ror_display": display,
        "n_points_used": n_used,
        "window_span_s": spans,
        "edge": edge,
        "window_s": cfg.window_s,
        "display_smooth_s": cfg.display_smooth_s,
        "min_points": cfg.min_points,
        "min_span_s": cfg.min_span_s,
    }


def _time_weighted_smooth(
    t: np.ndarray, ror: np.ndarray, smooth_s: float, measured: np.ndarray
) -> np.ndarray:
    """Centred trailing-mean of the RoR over ``smooth_s`` (display only).

    NaN RoR points (not enough data in window) are skipped rather than
    propagating.
    """
    out = np.full_like(ror, np.nan)
    half = smooth_s / 2.0
    valid = ~np.isnan(ror)
    for i in range(len(t)):
        if not measured[i]:
            continue
        m = valid & (np.abs(t - t[i]) <= half)
        if m.any():
            out[i] = float(np.mean(ror[m]))
    return out


def build_series(
    samples: list[dict],
    *,
    ror_cfg: RoRConfig,
    max_gap_fill_s: float,
) -> dict:
    """Assemble the full derived series for one batch from raw sample dicts.

    ``samples`` items need keys ``t_s``, ``bean_temp_c``, ``env_temp_c``.
    Measured temperatures stay untouched; all derived arrays are separate.
    """
    samples = sorted(samples, key=lambda s: s["t_s"])
    t = np.array([s["t_s"] for s in samples], dtype=float)
    bean = np.array(
        [np.nan if s["bean_temp_c"] is None else s["bean_temp_c"] for s in samples],
        dtype=float,
    )
    env = np.array(
        [np.nan if s["env_temp_c"] is None else s["env_temp_c"] for s in samples],
        dtype=float,
    )

    bean_filled, bean_interp, bean_gaps = _linear_fill(
        t, bean, max_gap_fill_s, channel="bean"
    )
    env_filled, env_interp, env_gaps = _linear_fill(
        t, env, max_gap_fill_s, channel="env"
    )

    ror = rate_of_rise(t, bean, ror_cfg)

    raw_points = [
        {
            "t_s": float(t[i]),
            "bean_temp_c": None if np.isnan(bean[i]) else float(bean[i]),
            "env_temp_c": None if np.isnan(env[i]) else float(env[i]),
            "ror_c_per_min": None if np.isnan(ror["ror_raw"][i]) else float(ror["ror_raw"][i]),
            "ror_display": None if np.isnan(ror["ror_display"][i]) else float(ror["ror_display"][i]),
            "ror_n_points": int(ror["n_points_used"][i]),
            "ror_edge": bool(ror["edge"][i]),
            "is_interpolated": bool(bean_interp[i]),
        }
        for i in range(len(t))
    ]

    return {
        "raw_points": raw_points,
        # Continuous guide line (measured + flagged interpolated), NaN across
        # unfilled gaps so the chart renders a break.
        "guide_bean_temp": [None if np.isnan(v) else float(v) for v in bean_filled],
        "guide_env_temp": [None if np.isnan(v) else float(v) for v in env_filled],
        "interpolated_t_s": [float(x) for x in t[bean_interp | env_interp]],
        "missing_segments": bean_gaps + env_gaps,
        "ror_window": {
            "method": "centred_least_squares_slope_on_measured_points",
            "window_s": ror["window_s"],
            "display_smooth_s": ror["display_smooth_s"],
            "min_points": ror["min_points"],
            "min_span_s": ror["min_span_s"],
            "units": "C/min",
        },
    }


def current_events(events: list[dict]) -> dict[str, dict]:
    """Map event_type -> the latest non-superseded event row (as dict)."""
    out: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (e["t_s"], e["id"])):
        if not e["superseded"]:
            out[e["event_type"]] = e
    return out


# ---------------------------------------------------------------------------
# Interruption ledger: wall-clock time vs active roasting time
# ---------------------------------------------------------------------------
#
# The roaster clock keeps running during a power cut or a safety inspection;
# the heating does not.  The interruption ledger records those intervals so we
# can report both durations honestly:
#
#   wall duration   = t1 - t0                (what a clock on the wall shows)
#   active duration = wall minus the union of closed interruption intervals
#                     that fall inside [t0, t1]
#
# A probe dropout (NULL sample) is *never* treated as an interruption — the
# ledger only contains explicit operator entries.  Nothing here guesses: every
# malformed condition (resume without a start, duplicate start/resume,
# overlapping intervals, negative duration ...) is returned as an explicit
# conflict and the affected active numbers stay null instead of being invented.

INTERRUPTION_TIME_BASIS = {
    "wall": "wall clock: t_end - t_start, never pauses",
    "active": (
        "active roasting: wall duration minus the union of CURRENT-VERSION, "
        "closed, non-overlapping interruption intervals strictly inside the span"
    ),
    "interval_convention": (
        "intervals are half-open [start_s, end_s); an anchor exactly at an "
        "interval start is INSIDE the interruption, an anchor exactly at the "
        "resume instant is active"
    ),
    "open_intervals": (
        "an unclosed interval is capped at the last measured sample; active "
        "duration is never extrapolated beyond the data"
    ),
    "probe_dropout_is_not_interruption": True,
}


def _current_rows(records: list[dict]) -> list[dict]:
    return sorted(
        (r for r in records if not r.get("superseded", False)),
        # At the same instant a close (resume/terminate) is ordered before a new
        # start: an interval that resumes exactly when another starts is legal
        # half-open touching, not a duplicate start.
        key=lambda r: (float(r["t_s"]), 1 if r["action"] == "start" else 0, r["id"]),
    )


def build_interruptions(
    records: list[dict],
    *,
    data_end_s: float | None = None,
) -> dict:
    """Validate the current-version ledger and derive interruption intervals.

    Pure function (same inputs -> same output), used by the API, the export and
    the independent recompute endpoint.  Only non-superseded rows participate;
    superseded rows belong to older versions and stay in storage/export.

    The replay is deliberately simple: walk the rows in wall-clock order and
    track which interval id is currently open.  Every illegal move becomes a
    structured conflict rather than an exception, so callers can render it.
    """
    rows = _current_rows(records)
    conflicts: list[dict] = []
    intervals: list[dict] = []
    open_row: dict | None = None
    closed_ids: set = set()
    batch_ended = False

    for r in rows:
        action = r["action"]
        iid = r.get("interval_id")
        if action == "start":
            if iid is None:
                conflicts.append({"kind": "start_without_interval_id", "record_id": r["id"]})
                continue
            if open_row is not None:
                conflicts.append({
                    "kind": "duplicate_start",
                    "record_id": r["id"],
                    "interval_id": iid,
                    "open_interval_id": open_row["interval_id"],
                    "message": "中断尚未恢复就又记录了一次中断开始",
                })
            elif iid in closed_ids:
                conflicts.append({
                    "kind": "duplicate_start",
                    "record_id": r["id"],
                    "interval_id": iid,
                    "message": "该中断区间已关闭，不能重复开始",
                })
                continue
            open_row = r
        elif action in ("resume", "terminate"):
            if action == "resume":
                if open_row is None or open_row.get("interval_id") != iid:
                    conflicts.append({
                        "kind": "orphan_resume",
                        "record_id": r["id"],
                        "interval_id": iid,
                        "t_s": float(r["t_s"]),
                        "message": "没有处于打开状态的中断区间，恢复请求被拒绝（不猜测归属）",
                    })
                    continue
                open_row = None
                closed_ids.add(iid)
            else:  # terminate
                batch_ended = True
                if iid is None:
                    # Normal end while roasting — nothing to close.
                    continue
                if open_row is not None and open_row.get("interval_id") == iid:
                    open_row = None
                    closed_ids.add(iid)
                elif iid not in closed_ids:
                    conflicts.append({
                        "kind": "orphan_terminate",
                        "record_id": r["id"],
                        "interval_id": iid,
                        "t_s": float(r["t_s"]),
                        "message": "终止记录指向一个不存在的中断区间",
                    })

    # Reassemble intervals from current rows (start + its closing row).
    starts = {r["interval_id"]: r for r in rows if r["action"] == "start"}
    closes = {}
    for r in rows:
        if r["action"] in ("resume", "terminate") and r.get("interval_id") is not None:
            # The FIRST close is the legal one; replay has already flagged any
            # later duplicate resume as an orphan.
            closes.setdefault(r["interval_id"], r)
    for iid, s in sorted(starts.items(), key=lambda kv: float(kv[1]["t_s"])):
        c = closes.get(iid)
        start_s = float(s["t_s"])
        end_s = float(c["t_s"]) if c is not None else None
        duration = None if end_s is None else round(end_s - start_s, 3)
        if end_s is not None and end_s <= start_s:
            conflicts.append({
                "kind": "non_positive_duration",
                "interval_id": iid,
                "start_s": start_s,
                "end_s": end_s,
                "message": "中断结束时刻不晚于开始时刻",
            })
        intervals.append({
            "interval_id": iid,
            "version": int(s.get("version", 1)),
            "start_s": start_s,
            "end_s": end_s,
            "close_action": None if c is None else c["action"],
            "duration_s": duration,
            "reason": s.get("reason", ""),
            "note": s.get("note", ""),
            "source": s.get("source", "manual"),
            "start_source": s.get("source", "manual"),
            "end_source": None if c is None else c.get("source", "manual"),
            "start_record_id": s["id"],
            "end_record_id": None if c is None else c["id"],
            "is_open": c is None,
        })

    intervals.sort(key=lambda iv: iv["start_s"])

    # Pairwise overlap, strict half-open semantics.  An open interval is capped
    # at the last measured sample for this comparison and never extended past it.
    cap = data_end_s
    for i in range(len(intervals)):
        for j in range(i + 1, len(intervals)):
            a, b = intervals[i], intervals[j]
            a_end = a["end_s"] if a["end_s"] is not None else cap
            b_end = b["end_s"] if b["end_s"] is not None else cap
            if a_end is None or b_end is None:
                continue  # nothing to cap an open interval against
            if a["start_s"] < b_end and b["start_s"] < a_end:
                conflicts.append({
                    "kind": "overlap",
                    "interval_ids": [a["interval_id"], b["interval_id"]],
                    "a": [a["start_s"], a["end_s"]],
                    "b": [b["start_s"], b["end_s"]],
                    "message": "两个中断区间相互重叠，无法确定活动时长（不猜测）",
                })

    if open_row is not None and batch_ended:
        conflicts.append({
            "kind": "ended_with_open_interval",
            "interval_id": open_row["interval_id"],
            "message": "批次已确认终止，但仍有未关闭的中断区间",
        })

    structural_kinds = {
        "orphan_resume", "orphan_terminate", "duplicate_start",
        "overlap", "non_positive_duration",
        "start_without_interval_id", "ended_with_open_interval",
    }
    computable = not any(c["kind"] in structural_kinds for c in conflicts)

    total_interrupted_s: float | None = None
    if computable:
        total = 0.0
        uncapable_open = False
        for iv in intervals:
            if iv["duration_s"] is not None:
                total += iv["duration_s"]
            elif cap is not None and cap > iv["start_s"]:
                total += cap - iv["start_s"]
            else:
                # open interval with no measured data to cap it: do not guess
                uncapable_open = True
        total_interrupted_s = None if uncapable_open else round(total, 3)

    if batch_ended:
        ledger_status = "ended"
    elif open_row is not None:
        ledger_status = "interrupted"
    elif intervals:
        ledger_status = "resumed"
    else:
        ledger_status = "in_progress"

    return {
        "computable": computable,
        "ledger_status": ledger_status,
        "intervals": intervals,
        "conflicts": conflicts,
        "total_interrupted_s": total_interrupted_s,
        "open_interval_id": None if open_row is None else open_row["interval_id"],
        "data_end_s": data_end_s,
        "time_basis": INTERRUPTION_TIME_BASIS,
    }


def _subtract_intervals(
    lo: float, hi: float, intervals: list[dict], data_end_s: float | None
) -> float | None:
    """Active seconds in wall span [lo, hi] after subtracting the union of
    interruption intervals.  Returns None only when an open interval cannot be
    capped by the measured data (never extrapolated)."""
    if hi < lo:
        return None
    removed = 0.0
    for iv in intervals:
        s = iv["start_s"]
        e = iv["end_s"]
        if e is None:
            if data_end_s is None:
                return None
            e = data_end_s
        ov_lo = max(lo, s)
        ov_hi = min(hi, e)
        if ov_hi > ov_lo:
            removed += ov_hi - ov_lo
    return round(max(hi - lo - removed, 0.0), 3)


def _anchor_in_interruption(
    t: float, intervals: list[dict], data_end_s: float | None
) -> dict | None:
    """Half-open [start, end): an anchor at the resume instant is active; an
    anchor at the interruption start (or anywhere inside) is not."""
    for iv in intervals:
        e = iv["end_s"] if iv["end_s"] is not None else data_end_s
        if e is None:
            continue
        if iv["start_s"] <= t < e:
            return iv
    return None


def phase_metrics(
    events: list[dict],
    interruptions: dict | None = None,
    *,
    data_end_s: float | None = None,
) -> dict:
    """Development-time ratio etc., computed over explicit event intervals.

    Two time bases are always reported side by side:

    * the top-level ``*_s`` fields and ``development_ratio`` are **wall clock**
      durations (unchanged historical behaviour);
    * the matching ``*_active_s`` fields and ``development_ratio_active``
      subtract ledger interruption intervals.  They are ``None`` whenever the
      span boundary sits inside an interruption or the ledger is not
      computable — never a guessed value.

    Intervals (anchored on operator-visible, source-labelled events):
      drying:      charge -> turning point
      maillard:    turning point -> first crack start
      development: first crack start -> drop
      total:       charge -> drop
      development_ratio = development / total.
    """
    cur = current_events(events)

    def pt(kind: str):
        e = cur.get(kind)
        return None if e is None else {
            "t_s": float(e["t_s"]),
            "source": e["source"],
            "event_id": e["id"],
        }

    charge = pt("charge")
    tp = pt("turning_point")
    fc = pt("first_crack_start")
    fce = pt("first_crack_end")
    drop = pt("drop")

    def span(a, b):
        if a is None or b is None:
            return None
        return round(b["t_s"] - a["t_s"], 3)

    drying = span(charge, tp)
    maillard = span(tp, fc)
    development = span(fc, drop)
    total = span(charge, drop)
    crack_window = span(fc, fce)

    ratio = None
    if development is not None and total not in (None, 0):
        ratio = round(development / total, 4)

    # ---- active-roasting basis -----------------------------------------
    active_status = "unavailable"
    active_blockers: list[dict] = []
    anchor_conflicts: list[dict] = []
    drying_active = maillard_active = development_active = total_active = None
    crack_window_active = None
    ratio_active = None
    interrupted_in_total = None

    interval_list = interruptions["intervals"] if interruptions else []
    ledger_conflict = bool(interruptions and not interruptions["computable"])

    def active_span(a, b, metric: str):
        nonlocal active_status
        if a is None or b is None:
            return None
        for anchor in (a, b):
            iv = _anchor_in_interruption(anchor["t_s"], interval_list, data_end_s)
            if iv is not None:
                active_blockers.append({
                    "metric": metric,
                    "reason": "anchor_inside_interruption",
                    "anchor_t_s": anchor["t_s"],
                    "interval_id": iv["interval_id"],
                    "message": "区间锚点落在中断期内，活动时长不可计算（不猜测）",
                })
                anchor_conflicts.append({
                    "interval_id": iv["interval_id"],
                    "metric": metric,
                    "anchor_t_s": anchor["t_s"],
                })
                return None
        return _subtract_intervals(a["t_s"], b["t_s"], interval_list, data_end_s)

    if interruptions is not None:
        if ledger_conflict:
            active_status = "conflict"
        else:
            drying_active = active_span(charge, tp, "drying")
            maillard_active = active_span(tp, fc, "maillard")
            development_active = active_span(fc, drop, "development")
            crack_window_active = active_span(fc, fce, "first_crack_window")
            total_active = active_span(charge, drop, "total")
            if total_active is not None and total is not None:
                interrupted_in_total = round(total - total_active, 3)
            if development_active is not None and total_active not in (None, 0):
                ratio_active = round(development_active / total_active, 4)
            if active_blockers:
                active_status = "partial"
            else:
                active_status = "ok"

    duration_table = []
    for key, label, definition, wall_v, active_v in [
        ("drying", "脱水期", "charge -> turning_point", drying, drying_active),
        ("maillard", "梅纳/反应期", "turning_point -> first_crack_start", maillard, maillard_active),
        ("development", "发展期", "first_crack_start -> drop", development, development_active),
        ("first_crack_window", "一爆持续", "first_crack_start -> first_crack_end",
         crack_window, crack_window_active),
        ("total", "总时长", "charge -> drop", total, total_active),
    ]:
        duration_table.append({
            "key": key,
            "label": label,
            "definition": definition,
            "wall_s": wall_v,
            "active_s": active_v,
            "interrupted_s": None
            if wall_v is None or active_v is None
            else round(wall_v - active_v, 3),
        })

    return {
        # Wall clock (unchanged keys — every historical consumer keeps working)
        "drying_s": drying,
        "maillard_s": maillard,
        "development_s": development,
        "first_crack_window_s": crack_window,
        "total_s": total,
        "development_ratio": ratio,
        # Active roasting basis
        "drying_active_s": drying_active,
        "maillard_active_s": maillard_active,
        "development_active_s": development_active,
        "first_crack_window_active_s": crack_window_active,
        "total_active_s": total_active,
        "development_ratio_active": ratio_active,
        "interrupted_within_total_s": interrupted_in_total,
        "active_metrics_status": active_status,
        "active_blockers": active_blockers,
        "anchor_conflicts": anchor_conflicts,
        "duration_table": duration_table,
        "interval_definition": {
            "drying": "charge -> turning_point",
            "maillard": "turning_point -> first_crack_start",
            "development": "first_crack_start -> drop",
            "development_ratio": "development_s / total_s (charge -> drop)",
            "development_ratio_active": (
                "development_active_s / total_active_s; null when any anchor is "
                "inside an interruption or the ledger conflicts"
            ),
        },
        "time_basis": {
            "wall_fields_suffix": "_s",
            "active_fields_suffix": "_active_s",
            **INTERRUPTION_TIME_BASIS,
        },
        "anchors": {
            "charge": charge,
            "turning_point": tp,
            "first_crack_start": fc,
            "first_crack_end": fce,
            "drop": drop,
        },
    }


def elapsed_summary(
    events: list[dict],
    interruptions: dict | None,
    data_end_s: float,
) -> dict:
    """Page-level clock answer: elapsed WALL time and elapsed ACTIVE roasting
    time up to the drop event (or the last measured sample while ongoing)."""
    cur = current_events(events)
    drop = cur.get("drop")
    wall_end = float(drop["t_s"]) if drop else float(data_end_s)
    charge = cur.get("charge")
    wall_start = float(charge["t_s"]) if charge else 0.0
    wall = round(wall_end - wall_start, 3)

    active = None
    status = "no_ledger"
    if interruptions is not None:
        if not interruptions["computable"]:
            status = "conflict"
        else:
            # An open interval is capped at data_end_s, so an in-progress
            # interruption is subtracted right up to "now" without extrapolating.
            active = _subtract_intervals(
                wall_start, wall_end, interruptions["intervals"], data_end_s
            )
            status = "interrupted_open" if interruptions["open_interval_id"] is not None else "ok"
    return {
        "wall_elapsed_s": wall,
        "active_elapsed_s": active,
        "excluded_interrupted_s": None if active is None else round(wall - active, 3),
        "ended_with_drop": drop is not None,
        "open_interval_id": interruptions["open_interval_id"] if interruptions else None,
        "status": status,
        "basis": "wall ends at drop event if present, otherwise at last measured sample",
    }


def detect_turning_point(samples: list[dict], after_s: float = 30.0) -> float | None:
    """Suggest a turning point: first local minimum of measured bean temp.

    Detection is a *suggestion* only; it is stored with source='auto' and the
    operator can supersede it with a manual event.
    """
    pts = sorted(
        (s for s in samples if s["bean_temp_c"] is not None),
        key=lambda s: s["t_s"],
    )
    pts = [s for s in pts if s["t_s"] >= after_s]
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        if a["bean_temp_c"] >= b["bean_temp_c"] < c["bean_temp_c"]:
            return float(b["t_s"])
    return None
