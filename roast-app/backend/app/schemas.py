"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .models import INTERRUPT_ACTIONS


class SampleIn(BaseModel):
    t_s: float
    bean_temp_c: float | None = None
    env_temp_c: float | None = None
    sampled_at: datetime | None = None


class EventIn(BaseModel):
    event_type: str
    t_s: float = Field(ge=0)
    label: str = ""
    source: str = "manual"
    created_by: str = "operator"
    value_num: float | None = None
    note: str = ""


class EventOut(EventIn):
    id: int
    batch_id: int
    superseded: bool
    superseded_by_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class BatchMeta(BaseModel):
    id: int
    name: str
    roaster: str
    bean: str
    charge_at: datetime
    charge_temp_c: float
    ambient_temp_c: float
    target_drop_temp_c: float | None = None
    note: str
    status: str = "in_progress"

    class Config:
        from_attributes = True


class InterruptIn(BaseModel):
    """Operator ledger entry.

    ``t_s`` is seconds since charge on the wall clock (it is a recorded moment
    in the roast, not active-roasting time).
    """

    action: str = Field(description="start | resume | terminate")
    t_s: float = Field(ge=0)
    reason: str = ""
    note: str = ""
    source: str = "manual"
    created_by: str = "operator"

    def checked_action(self) -> str:
        if self.action not in INTERRUPT_ACTIONS:
            raise ValueError(f"action must be one of {INTERRUPT_ACTIONS}")
        return self.action


class InterruptCorrectIn(BaseModel):
    """Backfill/correction of a whole interval as a new *version*.

    Provide the corrected start and the corrected close (resume = heating came
    back; terminate = the batch ended while it was interrupted).  Rows of the
    previous version are kept and marked superseded — never rewritten.
    """

    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    close_action: str = Field(description="resume | terminate")
    reason: str = ""
    note: str = ""
    source: str = "post_hoc"
    created_by: str = "operator"


class InterruptRecordOut(BaseModel):
    id: int
    batch_id: int
    action: str
    interval_id: int | None = None
    version: int
    t_s: float
    reason: str
    note: str
    source: str
    created_by: str
    superseded: bool
    superseded_by_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True
