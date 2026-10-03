"""NumPy analysis pipeline: rate-of-rise, gap handling, phase metrics,
interruption ledger interpretation.

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

Interruptions — wall-clock vs active roasting time
--------------------------------------------------
A probe dropout is NOT an interruption.  An interruption exists only when an
operator records that heating actually stopped (power cut, safety
inspection...), via the append-only interruption ledger.  Two time bases are
therefore always distinguished:

* **wall-clock** duration: plain ``t_end - t_start`` along the timeline;
* **active roasting** duration: wall-clock minus the union of *closed*
  interruption intervals contained in the span.

Nothing is guessed.  When the ledger itself is structurally invalid (a
resume with no start, two resumes, a resume before the start), when current
intervals overlap, or when a phase anchor sits strictly *inside* an
interruption band, the affected active-time outputs are returned as ``None``
with explicit conflict codes — never a fabricated number.  Wall-clock
metrics are independent of the ledger and are always returned.  A later
ledger *version* supersedes the current interpretation but never the
historical rows; metrics as-of an older ledger version remain reproducible.
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

# Conflict codes surfaced whenever an active-time result cannot be produced
# without guessing.  Wall-clock metrics are unaffected.
C_OVERLAP = "overlapping_interruptions"
C_ANCHOR_INSIDE = "phase_anchor_inside_interruption"
C_INTERVAL_OUTSIDE = "intervals_overlap_or_exceed_bounds"
C_LEDGER_STRUCTURE = "invalid_ledger_structure"

#: only one start; at most one resume/terminate each; closes must follow start.
_INTERVAL_ACTIONS = ("start", "resume", "terminate")


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
    interruption_records: list[dict] | None = None,
) -> dict:
    """Assemble the full derived series for one batch from raw sample dicts.

    ``samples`` items need keys ``t_s``, ``bean_temp_c``, ``env_temp_c``.
    Measured temperatures stay untouched; all derived arrays are separate.

    ``interruption_records`` (the operator ledger, optional) only adds visible
    heat-off bands and provenance metadata — it never removes or alters samples
    or gap handling.  Bands extend to the last measured sample while open.
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

    # Interruption bands are a separate, operator-sourced visual layer; they
    # never modify samples or the gap/interpolation audit.
    ledger = current_interruption_intervals(interruption_records or [])
    horizon = float(np.nanmax(t)) if len(t) else None
    bands = interruption_bands(ledger["intervals"], open_until_s=horizon)

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
        "interruption_bands": bands,
        "interruption_ledger": {
            "status": ledger["batch_status"],
            "open": ledger["open"],
            "computable": ledger["computable"],
            "conflicts": ledger["conflicts"],
            "note": (
                "中断带只来自操作员中断账本；探针失联(missing samples)不是中断，"
                "原始采样与缺测处理不受影响。"
            ),
        },
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
# interruption ledger: versioned intervals, conflicts, active time
# ---------------------------------------------------------------------------

def _row_key(r: dict) -> tuple:
    """Total order for ledger rows (created_at may be missing in dicts)."""
    return (
        r.get("created_at") or "",
        r.get("id", 0),
    )


def interruption_versions(records: list[dict]) -> list[dict]:
    """Reconstruct every ledger *version*, newest first, for audit history.

    Each returned version is self-contained:

      ``version``              – version number,
      ``as_of``                – created_at of the version's rows,
      ``interval_id``          – logical episode this version belongs to,
      ``records``              – rows inserted at that version (start/resume…),
      ``start_s``/``end_s``    – interval bounds (end_s may be None = open),
      ``open``                 – heat still off in that version,
      ``superseded``           – a newer version exists for the same episode.

    Rows are grouped by ``interval_id`` and ordered by ``version`` then
    creation order, so an old interval — and the metrics reported with it —
    can be reproduced at any time even after a correction.
    """
    by_group: dict[str | None, list[dict]] = {}
    for r in records:
        by_group.setdefault(r.get("interval_id"), []).append(r)

    versions: list[dict] = []
    for interval_id, rows in by_group.items():
        versions_by_n: dict[int, list[dict]] = {}
        for r in rows:
            versions_by_n.setdefault(int(r.get("version") or 1), []).append(r)
        max_v = max(versions_by_n)
        for v, vrows in versions_by_n.items():
            vrows = sorted(vrows, key=_row_key)
            start = next((x for x in vrows if x["action"] == "start"), None)
            end = None
            for act in ("resume", "terminate"):
                hit = next((x for x in vrows if x["action"] == act), None)
                if hit is not None:
                    end = hit
                    break
            # as_of = moment the last row of THIS version existed, so an as-of
            # reconstruction sees the interval exactly as the version defined it.
            as_of = max(
                (x.get("created_at") or "" for x in vrows),
                default="",
            )
            versions.append(
                {
                    "interval_id": interval_id,
                    "version": v,
                    "as_of": as_of or None,
                    "records": [_ledger_row_brief(x) for x in vrows],
                    "start_s": None if start is None else float(start["t_s"]),
                    "end_s": None if end is None else float(end["t_s"]),
                    "end_action": None if end is None else end["action"],
                    "open": start is not None and end is None,
                    "superseded": v < max_v,
                }
            )
    versions.sort(key=lambda v: v["as_of"] or "", reverse=True)
    return versions


def _ledger_row_brief(r: dict) -> dict:
    return {
        "id": r.get("id"),
        "action": r["action"],
        "t_s": float(r["t_s"]),
        "reason": r.get("reason", ""),
        "source": r.get("source", "manual"),
        "created_by": r.get("created_by", ""),
        "version": int(r.get("version") or 1),
        "note": r.get("note", ""),
        "superseded": bool(r.get("superseded", False)),
        "superseded_by_id": r.get("superseded_by_id"),
        "created_at": r.get("created_at"),
    }


def current_interruption_intervals(
    records: list[dict],
    *,
    as_of: str | None = None,
    event_times: dict[str, float] | None = None,
) -> dict:
    """Interpret the current (or as-of) interruption ledger.

    Returns::

        {
          "intervals": [   # current versions only, time-ordered
            {"interval_id", "version", "start_s", "end_s" (None=open),
             "open", "start_reason", ...}
          ],
          "open": bool,                    # heat currently off
          "batch_status": "interrupted"|"resumed"|"in_progress"|"ended",
          "conflicts": [{"code", "message", "interval_id"?}],
          "computable": bool,              # active roasting time trustworthy
          "ledger_version_basis": "current"|"as_of:<iso>",
        }

    Structural problems (close without start, multiple starts/resumes,
    resume before start, overlapping current intervals) are *reported*, never
    silently repaired.  ``computable`` is False when active roasting time
    cannot be derived; consumers must then emit ``None`` for active metrics.

    When ``as_of`` is given (an ISO timestamp string), only ledger rows with
    ``created_at <= as_of`` participate — that is how a historical version of
    the metrics is reconstructed for audit.  Event anchor times can be passed
    in ``event_times`` to additionally flag an anchor sitting strictly inside
    an interruption band (boundaries are allowed: stopping/resuming exactly at
    an event is an operator-recorded fact, not a conflict).
    """
    basis = f"as_of:{as_of}" if as_of else "current"
    if as_of:
        # Historical reconstruction: take the ledger rows that EXISTED at that
        # time, regardless of whether a later correction has since superseded
        # them.  Temporal filtering only — superseded markers belong to the
        # present, not to the as-of state.
        rows = [r for r in records if (r.get("created_at") or "") <= as_of]
    else:
        rows = [r for r in records if not r.get("superseded", False)]

    by_group: dict[str | None, list[dict]] = {}
    for r in rows:
        by_group.setdefault(r.get("interval_id"), []).append(r)

    conflicts: list[dict] = []
    intervals: list[dict] = []
    any_open = False
    terminated = False
    # A batch-level terminate (interval_id NULL) has no interval but still
    # marks the batch as ended.
    null_term = [
        r for r in rows if r.get("interval_id") is None and r["action"] == "terminate"
    ]
    if null_term:
        terminated = True

    for interval_id, grows in by_group.items():
        grows = sorted(grows, key=_row_key)
        starts = [r for r in grows if r["action"] == "start"]
        resumes = [r for r in grows if r["action"] == "resume"]
        terminates = [r for r in grows if r["action"] == "terminate"]
        closes = resumes + terminates

        cid = {"interval_id": interval_id}
        if len(starts) == 0:
            # A resume/terminate with no start: do not invent a start time.
            act = grows[0]["action"]
            conflicts.append(
                {
                    "code": C_LEDGER_STRUCTURE,
                    **cid,
                    "message": f"存在“{act}”记录但没有对应的中断开始记录，拒绝猜测开始时间。",
                }
            )
            continue
        if len(starts) > 1:
            conflicts.append(
                {"code": C_LEDGER_STRUCTURE, **cid,
                 "message": "同一逻辑中断存在多条开始记录。"}
            )
        if len(resumes) > 1 or len(terminates) > 1:
            conflicts.append(
                {"code": C_LEDGER_STRUCTURE, **cid,
                 "message": "同一逻辑中断存在重复的恢复/终止记录。"}
            )
        start = starts[0]
        end = closes[0] if closes else None
        if end is not None and float(end["t_s"]) < float(start["t_s"]):
            conflicts.append(
                {"code": C_LEDGER_STRUCTURE, **cid,
                 "message": "中断结束时间早于开始时间。"}
            )
            continue
        if terminates:
            terminated = True
        is_open = end is None
        any_open |= is_open
        intervals.append(
            {
                "interval_id": interval_id,
                "version": int(start.get("version") or 1),
                "start_s": float(start["t_s"]),
                "end_s": None if end is None else float(end["t_s"]),
                "end_action": None if end is None else end["action"],
                "open": is_open,
                "start_reason": start.get("reason", ""),
                "end_reason": "" if end is None else end.get("reason", ""),
                "source": start.get("source", "manual"),
                "created_by": start.get("created_by", ""),
                "start_record": _ledger_row_brief(start),
                "end_record": None if end is None else _ledger_row_brief(end),
            }
        )

    intervals.sort(key=lambda iv: (iv["start_s"], iv["end_s"] is None, iv["end_s"] or 0.0))

    # Overlap of positive measure between current intervals.  Open ends are
    # treated as +infinity; touching at an endpoint is allowed (a resume and
    # another start recorded at the same instant is still one legal fact line).
    inf = float("inf")
    for i in range(len(intervals)):
        a = intervals[i]
        for j in range(i + 1, len(intervals)):
            b = intervals[j]
            a_hi = inf if a["end_s"] is None else a["end_s"]
            b_hi = inf if b["end_s"] is None else b["end_s"]
            if max(a["start_s"], b["start_s"]) < min(a_hi, b_hi):
                conflicts.append(
                    {
                        "code": C_OVERLAP,
                        "interval_id": a["interval_id"],
                        "other_interval_id": b["interval_id"],
                        "message": "两个中断区间时间重叠，无法唯一扣除加热停顿时长。",
                    }
                )

    structure_bad = any(c["code"] == C_LEDGER_STRUCTURE for c in conflicts)
    overlap_bad = any(c["code"] == C_OVERLAP for c in conflicts)

    if any_open:
        status = "interrupted"
    elif terminated:
        status = "ended"
    elif intervals:
        status = "resumed"
    else:
        status = "in_progress"

    out = {
        "intervals": intervals,
        "open": any_open,
        "batch_status": status,
        "conflicts": conflicts,
        "computable": not (structure_bad or overlap_bad),
        "ledger_version_basis": basis,
    }

    if event_times:
        out["anchor_conflicts"], out["anchor_notes"] = _check_anchors(
            intervals, event_times
        )
        if out["anchor_conflicts"]:
            out["conflicts"].extend(out["anchor_conflicts"])
    return out


def _check_anchors(
    intervals: list[dict], event_times: dict[str, float]
) -> tuple[list[dict], list[dict]]:
    """Flag phase anchors strictly inside a closed/open interruption band.

    An anchor exactly on a start/end boundary is allowed and returned as a
    note; one strictly inside means the operator placed a process event while
    the heat was off, so the adjacent phase durations cannot be interpreted —
    they are flagged and active DTR is withheld.
    """
    conflicts: list[dict] = []
    notes: list[dict] = []
    eps = 1e-9
    for name, t in event_times.items():
        if t is None:
            continue
        for iv in intervals:
            lo, hi = iv["start_s"], iv["end_s"]
            if hi is None:
                inside = t > lo + eps
                on = abs(t - lo) <= eps
            else:
                inside = lo + eps < t < hi - eps
                on = abs(t - lo) <= eps or abs(t - hi) <= eps
            if inside:
                conflicts.append(
                    {
                        "code": C_ANCHOR_INSIDE,
                        "anchor": name,
                        "t_s": round(float(t), 3),
                        "interval_id": iv["interval_id"],
                        "message": (
                            f"锚点“{name}”(t={round(float(t), 1)}s) 落在中断区间 "
                            f"[{round(lo, 1)}, "
                            f"{'开放' if hi is None else round(hi, 1)}] 内部，"
                            "相邻阶段活动时长与 DTR 不可计算。"
                        ),
                    }
                )
            elif on:
                notes.append(
                    {
                        "anchor": name,
                        "t_s": round(float(t), 3),
                        "interval_id": iv["interval_id"],
                        "boundary": "start" if abs(t - lo) <= eps else "end",
                        "message": f"锚点“{name}”与中断边界重合（记录允许），中断时长不计入活动时长。",
                    }
                )
    return conflicts, notes


def interruption_duration_in_span(
    intervals: list[dict], t0: float, t1: float
) -> float | None:
    """Union of interruption time inside ``[t0, t1]``.

    Open intervals intersecting the span make the result ``None``: active time
    through an as-yet-unclosed interruption cannot be stated without guessing
    the resume time.  Callers must have already checked ledger/overlap
    conflicts (``computable``).
    """
    if t0 is None or t1 is None or t1 < t0:
        return None
    cuts: list[tuple[float, float]] = []
    for iv in intervals:
        lo, hi = iv["start_s"], iv["end_s"]
        if hi is None:
            if lo < t1:  # open band reaches (or starts within) the span
                return None
            continue
        a, b = max(lo, t0), min(hi, t1)
        if b > a:
            cuts.append((a, b))
    if not cuts:
        return 0.0
    cuts.sort()
    total = 0.0
    ca, cb = cuts[0]
    for a, b in cuts[1:]:
        if a <= cb:
            cb = max(cb, b)
        else:
            total += cb - ca
            ca, cb = a, b
    total += cb - ca
    return round(total, 3)


def interruption_bands(
    intervals: list[dict],
    *,
    open_until_s: float | None = None,
) -> list[dict]:
    """Visible bands for the chart/phase table.

    Closed intervals render ``[start_s, end_s]``.  An open band is drawn up to
    ``open_until_s`` (typically the latest measured sample) purely as a visual
    guide and carries ``open=true``; it is never used as a measured duration.
    """
    bands = []
    for iv in intervals:
        end = iv["end_s"]
        open_now = iv["open"]
        if open_now and open_until_s is not None and open_until_s > iv["start_s"]:
            end = float(open_until_s)
        bands.append(
            {
                "interval_id": iv["interval_id"],
                "version": iv.get("version"),
                "start_s": iv["start_s"],
                "end_s": end,
                "open": open_now,
                "start_reason": iv.get("start_reason", ""),
                "end_action": iv.get("end_action"),
                "source": iv.get("source", "manual"),
            }
        )
    return bands


def phase_metrics(
    events: list[dict],
    interruption_records: list[dict] | None = None,
    *,
    active_horizon_s: float | None = None,
    as_of: str | None = None,
) -> dict:
    """Development-time ratio etc., computed over explicit event intervals.

    Intervals (all anchored on operator-visible, source-labelled events):
      drying:      charge -> turning point
      maillard:    turning point -> first crack start
      development: first crack start -> drop
      total:       charge -> drop

    Two time bases are reported side by side:
      ``*_s`` / ``development_ratio``  – **wall-clock**, plain t1-t0,
      ``*_active_s`` / ``development_ratio_active`` – **active roasting
      time**, excluding closed interruption intervals.

    Wall-clock numbers are always given when the anchors exist.  Active
    numbers are ``None`` whenever the ledger is structurally invalid, current
    intervals overlap, an anchor lies inside an interruption band, or an open
    interruption intersects the span — the code never guesses a resume time.

    Returns the exact events/interval basis used so the computation is
    auditable.  ``as_of`` reconstructs metrics against the ledger state at an
    earlier time (for post-hoc correction history).
    """
    cur = current_events(events)

    def pt(kind: str):
        e = cur.get(kind)
        return None if e is None else {"t_s": float(e["t_s"]), "source": e["source"]}

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

    # --- active roasting time basis --------------------------------------
    anchor_times = {
        "charge": None if charge is None else charge["t_s"],
        "turning_point": None if tp is None else tp["t_s"],
        "first_crack_start": None if fc is None else fc["t_s"],
        "first_crack_end": None if fce is None else fce["t_s"],
        "drop": None if drop is None else drop["t_s"],
    }
    ledger = current_interruption_intervals(
        interruption_records or [],
        as_of=as_of,
        event_times=anchor_times,
    )
    intervals = ledger["intervals"]
    anchor_conflicts = ledger.get("anchor_conflicts", [])
    anchor_names_inside = {c["anchor"] for c in anchor_conflicts}
    # A phase is tainted when either of its two anchors sits inside a band.
    tainted = {
        "drying_s": bool(anchor_names_inside & {"charge", "turning_point"}),
        "maillard_s": bool(anchor_names_inside & {"turning_point", "first_crack_start"}),
        "development_s": bool(anchor_names_inside & {"first_crack_start", "drop"}),
        "first_crack_window_s": bool(
            anchor_names_inside & {"first_crack_start", "first_crack_end"}
        ),
        "total_s": bool(anchor_names_inside & {"charge", "drop"}),
    }

    def active_span(a, b, key: str):
        wall = span(a, b)
        if wall is None or not ledger["computable"] or tainted[key]:
            return None
        off = interruption_duration_in_span(intervals, a["t_s"], b["t_s"])
        if off is None:
            return None
        return round(wall - off, 3)

    drying_a = active_span(charge, tp, "drying_s")
    maillard_a = active_span(tp, fc, "maillard_s")
    development_a = active_span(fc, drop, "development_s")
    crack_a = active_span(fc, fce, "first_crack_window_s")
    total_a = active_span(charge, drop, "total_s")

    ratio_active = None
    if development_a is not None and total_a not in (None, 0):
        ratio_active = round(development_a / total_a, 4)

    total_interrupted_s = interruption_duration_in_span(
        intervals,
        charge["t_s"] if charge else 0.0,
        drop["t_s"] if drop else (active_horizon_s or 0.0),
    ) if ledger["computable"] and charge and (drop or active_horizon_s is not None) else None

    # Open interruption currently intersecting the batch timeline.
    open_iv = next((iv for iv in intervals if iv["open"]), None)

    return {
        # wall-clock basis (ledger-independent; always reported)
        "drying_s": drying,
        "maillard_s": maillard,
        "development_s": development,
        "first_crack_window_s": crack_window,
        "total_s": total,
        "development_ratio": ratio,
        # active roasting basis (ledger-derived; None when not computable)
        "drying_active_s": drying_a,
        "maillard_active_s": maillard_a,
        "development_active_s": development_a,
        "first_crack_window_active_s": crack_a,
        "total_active_s": total_a,
        "development_ratio_active": ratio_active,
        "total_interrupted_s": total_interrupted_s,
        "active_time_computable": ledger["computable"] and not anchor_conflicts,
        "open_interruption": (
            None if open_iv is None
            else {"interval_id": open_iv["interval_id"], "start_s": open_iv["start_s"]}
        ),
        "time_basis": {
            "wall_clock": "t_end - t_start along the recorded timeline (includes interruptions).",
            "active_roasting": (
                "wall-clock minus the union of CLOSED interruption intervals; "
                "None while an interruption is open or when conflicts exist."
            ),
            "probe_dropout_is_not_interruption": (
                "A NULL probe sample is missing measurement data, never a heat "
                "stop; interruptions exist only in the operator ledger."
            ),
        },
        "interval_definition": {
            "drying": "charge -> turning_point",
            "maillard": "turning_point -> first_crack_start",
            "development": "first_crack_start -> drop",
            "development_ratio": "development_s / total_s",
            "development_ratio_active": (
                "development_active_s / total_active_s (closed interruptions excluded)"
            ),
        },
        "anchors": {
            "charge": charge,
            "turning_point": tp,
            "first_crack_start": fc,
            "first_crack_end": fce,
            "drop": drop,
        },
        "interruption_basis": {
            "ledger_version_basis": ledger["ledger_version_basis"],
            "computable": ledger["computable"],
            "conflicts": ledger["conflicts"],
            "anchor_notes": ledger.get("anchor_notes", []),
            "n_intervals": len(intervals),
        },
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
