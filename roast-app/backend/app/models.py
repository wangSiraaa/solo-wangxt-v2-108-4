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
* ``interrupt_records`` is an append-only ledger of heating interruptions
  (power cut, safety inspection ...).  Every row carries its action
  (start / resume / terminate), reason, source and *version*.  A post-hoc
  correction never rewrites a row: it marks the old rows ``superseded`` and
  inserts version+1, so the interval as it was known then — and the metrics
  computed from it — stay auditable forever.

A probe dropout (NULL sample) is deliberately NOT an interruption: losing the
probe signal says nothing about whether the burner was on.  Interruptions are
only created by explicit operator ledger entries.
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

# Legal batch lifecycle.  in_progress -> interrupted -> resumed -> ... -> ended
BATCH_IN_PROGRESS = "in_progress"
BATCH_INTERRUPTED = "interrupted"
BATCH_RESUMED = "resumed"
BATCH_ENDED = "ended"
BATCH_STATUSES = (
    BATCH_IN_PROGRESS,
    BATCH_INTERRUPTED,
    BATCH_RESUMED,
    BATCH_ENDED,
)

# Ledger actions.
ACTION_START = "start"
ACTION_RESUME = "resume"
ACTION_TERMINATE = "terminate"
INTERRUPT_ACTIONS = (ACTION_START, ACTION_RESUME, ACTION_TERMINATE)


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
    # Lifecycle state derived from (and validated against) the interruption
    # ledger; see status transitions at the top of this module.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BATCH_IN_PROGRESS,
        server_default=BATCH_IN_PROGRESS, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    samples: Mapped[list["Sample"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Sample.t_s"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="Event.t_s"
    )
    interrupt_records: Mapped[list["InterruptRecord"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan",
        order_by="InterruptRecord.t_s",
    )


class Sample(Base):
    """One raw probe reading.  Temperatures are NULL when the probe was
    briefly lost — the missing reading is preserved as missing, not invented."""

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


class InterruptRecord(Base):
    """One append-only entry in the interruption ledger.

    An *interval* is the pair of rows sharing one ``interval_id``: a ``start``
    row and (later) a ``resume`` or a ``terminate`` row.  A start without a
    close is an open interval (the batch is interrupted right now).

    A post-hoc correction inserts a new pair with ``version`` incremented and
    marks the old pair ``superseded`` — rows are never deleted or rewritten,
    exactly like ``Event``.
    """

    __tablename__ = "interrupt_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    # start | resume | terminate
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Groups the start row with its close.  NULL only for a terminate that ends
    # the batch while no interruption was open.
    interval_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 1 for the live entry, 2+ for corrected versions of the same interval.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    t_s: Mapped[float] = mapped_column(Float, nullable=False)
    # power_outage | safety_inspection | gas_supply | operator_break | ...
    reason: Mapped[str] = mapped_column(String(60), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    # manual = operator on the floor; post_hoc = backfilled correction;
    # auto is intentionally NOT offered: the system never opens an interval by
    # itself (a probe dropout is not evidence the heating stopped).
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    superseded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("interrupt_records.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    batch: Mapped[Batch] = relationship(back_populates="interrupt_records")


_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    # Forward-compatible migration for databases created before the ledger
    # existed: add batches.status in place without touching any other table.
    # (create_all only creates missing *tables*, it never alters one.)
    cols = {c["name"] for c in inspect(engine).get_columns("batches")}
    if "status" not in cols:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE batches ADD COLUMN status VARCHAR(20) NOT NULL "
                    "DEFAULT 'in_progress'"
                )
            )
