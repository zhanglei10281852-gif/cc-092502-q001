from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

KIND_PATTERN = "^(layer|ash_pit|channel|feature)$"
RELATION_PATTERN = "^(cuts|earlier|equals)$"


class SquareCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=40)
    name: str = Field(default="", max_length=120)


class UnitCreate(BaseModel):
    square_id: int
    number: str = Field(..., min_length=1, max_length=40)
    kind: str = Field(..., pattern=KIND_PATTERN)
    label: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2000)


class UnitUpdate(BaseModel):
    expected_version: int = Field(..., ge=1)
    kind: str | None = Field(default=None, pattern=KIND_PATTERN)
    label: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    square_id: int | None = None


class SealRequest(BaseModel):
    expected_version: int = Field(..., ge=1)


class CorrectRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1, max_length=500)
    kind: str | None = Field(default=None, pattern=KIND_PATTERN)
    label: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    square_id: int | None = None


class ReviewRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    note: str = Field(default="", max_length=500)


class RelationCreate(BaseModel):
    from_unit: int
    to_unit: int
    relation: str = Field(..., pattern=RELATION_PATTERN)


class ImportSquareRow(BaseModel):
    op: Literal["square"]
    code: str = Field(..., min_length=1, max_length=40)
    name: str = Field(default="", max_length=120)


class ImportUnitRow(BaseModel):
    op: Literal["unit"]
    number: str = Field(..., min_length=1, max_length=40)
    kind: str = Field(..., pattern=KIND_PATTERN)
    square: str = Field(..., min_length=1, max_length=40)
    label: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2000)


class ImportRelationRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    op: Literal["relation"]
    from_number: str = Field(..., alias="from", min_length=1, max_length=40)
    to_number: str = Field(..., alias="to", min_length=1, max_length=40)
    relation: str = Field(..., pattern=RELATION_PATTERN)


ImportRow = Annotated[Union[ImportSquareRow, ImportUnitRow, ImportRelationRow], Field(discriminator="op")]


class ImportRequest(BaseModel):
    rows: list[ImportRow] = Field(..., min_length=1, max_length=500)
