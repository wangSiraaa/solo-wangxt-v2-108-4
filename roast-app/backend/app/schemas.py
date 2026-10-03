"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class InterruptionIn(BaseModel):
    """One lifecycle action on the interruption ledger."""

    action: Literal["start", "resume", "terminate"]
    t_s: float = Field(ge=0)
    reason: str = ""
    source: str = "manual"
    created_by: str = "operator"
    note: str = ""


class InterruptionCorrectIn(BaseModel):
    """Backdated correction of a logical interruption: inserts a NEW version
    and supersedes the old rows without deleting them."""

    start_s: float = Field(ge=0)
    # Closed corrected interval; omit/None while heat is still off.
    end_s: float | None = Field(default=None, ge=0)
    end_action: Literal["resume", "terminate"] = "resume"
    reason: str = ""
    end_reason: str = ""
    source: str = "manual"
    created_by: str = "operator"
    note: str = ""

    @model_validator(mode="after")
    def _check_bounds(self) -> "InterruptionCorrectIn":
        if self.end_s is not None and self.end_s <= self.start_s:
            raise ValueError("end_s must be greater than start_s")
        return self


class InterruptionRecordOut(BaseModel):
    id: int
    batch_id: int
    interval_id: str | None = None
    version: int
    action: str
    t_s: float
    reason: str
    source: str
    created_by: str
    note: str
    superseded: bool
    superseded_by_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True
