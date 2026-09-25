"""田野上下文模块的请求模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

UNIT_TYPES = ("trench", "excavation_unit", "layer", "feature", "paleochannel")
RELATION_KINDS = ("earlier", "cuts", "equivalent")

UnitType = Literal["trench", "excavation_unit", "layer", "feature", "paleochannel"]
RelationKind = Literal["earlier", "cuts", "equivalent"]


class UnitCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9:_\-./]*$")
    unit_type: UnitType
    title: str = Field(default="", max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)


class UnitUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    attributes: dict[str, Any] | None = None
    base_version: int | None = Field(default=None, ge=1)


class CorrectionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    attributes: dict[str, Any] | None = None
    change_reason: str = Field(..., min_length=1, max_length=500)
    base_version: int = Field(..., ge=1)


class RelationCreate(BaseModel):
    source: str = Field(..., min_length=1, max_length=40)
    target: str = Field(..., min_length=1, max_length=40)
    kind: RelationKind
    evidence: str = Field(default="", max_length=1000)
    attributes: dict[str, Any] = Field(default_factory=dict)


class RelationReview(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=1000)


class ImportUnit(BaseModel):
    code: str = Field(..., min_length=1, max_length=40, pattern=r"^[A-Za-z0-9][A-Za-z0-9:_\-./]*$")
    unit_type: UnitType
    title: str = Field(default="", max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)


class ImportRelation(BaseModel):
    source: str = Field(..., min_length=1, max_length=40)
    target: str = Field(..., min_length=1, max_length=40)
    kind: RelationKind
    evidence: str = Field(default="", max_length=1000)


class ImportBatch(BaseModel):
    units: list[ImportUnit] = Field(default_factory=list)
    relations: list[ImportRelation] = Field(default_factory=list)
