from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class InventoryStart(BaseModel):
    session_code: str | None = Field(default=None, min_length=3, max_length=64)
    location_id: int = Field(gt=0)


class InventoryCount(BaseModel):
    sample_id: int = Field(gt=0)
    observed_present: bool
    observed_quantity: float | None = Field(default=None, ge=0)
    note: str = Field(default="", max_length=500)


class DispositionCreate(BaseModel):
    sample_id: int = Field(gt=0)
    action: Literal["review", "relocate", "adjust_quantity", "report_loss"]
    target_quantity: float | None = Field(default=None, ge=0)
    target_location_id: int | None = Field(default=None, gt=0)
    evidence_summary: str = Field(min_length=1, max_length=2000)
    responsibility_note: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def ensure_action_target(self):
        if self.action == "adjust_quantity" and self.target_quantity is None:
            raise ValueError("数量调整必须填写调整后数量")
        if self.action == "relocate" and self.target_location_id is None:
            raise ValueError("移位处置必须选择目标位置")
        return self


class RereviewConfirm(BaseModel):
    note: str = Field(default="", max_length=500)


class CollectionCreate(BaseModel):
    field_code: str = Field(min_length=3, max_length=100)
    project_code: str = Field(min_length=2, max_length=64)
    collected_by: str = Field(min_length=1, max_length=100)
    collected_at: str = Field(min_length=10, max_length=40)
    source_kind: str = Field(min_length=1, max_length=100)
    source_reference: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    preservation: str = Field(min_length=1, max_length=200)


class TransferCreate(BaseModel):
    location_id: int = Field(gt=0)
    expected_version: int = Field(gt=0)
    reason: str = Field(min_length=2, max_length=500)
    correlation_id: str | None = Field(default=None, max_length=100)


class DestructionExecute(BaseModel):
    method: str = Field(min_length=2, max_length=200)
    witness_one: int = Field(gt=0)
    witness_two: int = Field(gt=0)
