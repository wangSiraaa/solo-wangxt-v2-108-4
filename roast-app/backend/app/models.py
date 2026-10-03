"""SQLAlchemy models: batches, *raw* samples, sourced events, and the
auditable interruption ledger.

Design rules enforced at the storage layer:

* ``samples`` only ever contains measured samples.  Interpolation for gaps is
  computed at query time and is returned with ``is_interpolated=True`` — it is
  never written back here, so a plotting convenience can never masquerade as a
  measurement.
* Events (turning point, first crack, damper change, drop ...) are append-only.
  A manual correction supersedes the previous row instead of deleting it, so
  every value keeps its ``source`` / ``created_by`` provenance.
* Interruption records (heat stopped for power/safety reasons) form a *separate*
  append-only ledger.  A probe dropout is NOT an interruption: only an operator
  record is.  Every ledger row carries its source/reason/version/status, and a
  backdated correction writes a NEW version (new rows) while the old rows stay
  in place, superseded but never deleted, so the intervals and metrics that
  were reported at the time remain auditable.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import DATABASE_URL

# Legal batch lifecycle states.  The ledger state machine (see main.py) only
# permits: in_progress -> interrupted -> resumed -> ended, with
# in_progress/resumed -> ended also legal (termination without an open
# interruption simply confirms the batch is over).
BATCH_STATES = ("in_progress", "interrupted", "resumed", "ended")


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    roaster: Mapped[str] = mapped_column(String(120), default="synthetic")
    bean: Mapped[str] = mapped_column(String(120), default="")
    charge_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    charge_temp_c: Mapped[float] = mapped_column(Float)
    ambient_temp_c: Mapped[float] = mapped_column(Float)
    target_drop_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Lifecycle state, driven exclusively by the interruption ledger.
    status: Mapped[str] = mapped_column(String(20), default="in_progress", nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    samples: Mapped[list["Sample"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Sample.t_s"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Event.t_s"
    )
    interruptions: Mapped[list["InterruptionRecord"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="InterruptionRecord.t_s",
    )


class Sample(Base):
    """One raw probe reading.  Temperatures are NULL when the probe was
    briefly lost — the missing reading is preserved as missing, not invented.

    A NULL reading is a *probe dropout*, never an interruption.  Interruptions
    (heat actually stopped: power cut, safety inspection) live in their own
    operator-written ledger and must not be inferred from missing samples."""

    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("batch_id", "t_s", name="uq_sample_batch_t"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # Seconds since charge.  Intervals are intentionally uneven.
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bean_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    env_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)

    batch: Mapped[Batch] = relationship(back_populates="samples")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # turning_point | first_crack_start | first_crack_end | drop |
    # damper_change | charge | custom
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    # auto = detected from the raw series; manual = operator entry.
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    # Numeric payload, e.g. new damper position (%) for damper_change.
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[Batch] = relationship(back_populates="events")


class InterruptionRecord(Base):
    """One append-only row of the interruption ledger.

    ``action`` is the lifecycle verb this row records:
      * ``start``     — heating stopped at ``t_s`` (batch becomes interrupted);
      * ``resume``    — heating resumed at ``t_s`` (batch becomes resumed);
      * ``terminate`` — operator confirms the batch ended at ``t_s``; when it
                        closes an open interruption the heat-off interval ends
                        here, otherwise it only marks the batch as ended.

    Rows belonging to the same logical heat-off episode share ``interval_id``
    and an increasing ``version``.  A backdated correction inserts a fresh
    start/resume(/terminate) pair at ``version + 1`` and marks the previous
    rows ``superseded=True`` (with ``superseded_by_id`` pointing at the new
    start row) — the old interval stays queryable forever.
    """

    __tablename__ = "interruption_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # UUID grouping the start/resume(/terminate) rows of one logical episode.
    # NULL only for a terminate that ends a batch with no open interruption.
    interval_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # start | resume | terminate
    action: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    # Why the heat stopped / resumed, e.g. power_cut | safety_check | schedule.
    reason: Mapped[str] = mapped_column(String(80), default="")
    # manual = operator entry; auto = system-generated (reserved).
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    note: Mapped[str] = mapped_column(Text, default="")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("interruption_records.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[Batch] = relationship(back_populates="interruptions")


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)


def _migrate_batch_lifecycle() -> None:
    """Bring pre-existing databases up to date without touching any data.

    The interruption feature adds ``batches.status`` / ``batches.ended_at`` and
    the ``interruption_records`` table.  Raw samples and existing events are
    never deleted or rewritten; an already-finished batch defaults to
    ``in_progress`` and the operator can reconcile it via the ledger.
    """
    inspector = inspect(engine)
    # Emit dialect-appropriate DDL for raw ALTERs (Postgres knows TIMESTAMP,
    # not DATETIME; SQLite accepts both).
    datetime_type = "DATETIME" if engine.dialect.name == "sqlite" else "TIMESTAMP"
    with engine.begin() as conn:
        if "batches" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("batches")}
            if "status" not in cols:
                conn.execute(
                    text("ALTER TABLE batches ADD COLUMN status VARCHAR(20) "
                         "NOT NULL DEFAULT 'in_progress'")
                )
            if "ended_at" not in cols:
                conn.execute(
                    text(f"ALTER TABLE batches ADD COLUMN ended_at {datetime_type}")
                )


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_batch_lifecycle()
