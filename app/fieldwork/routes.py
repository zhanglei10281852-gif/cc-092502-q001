from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query

from app.deps import current_user
from app.fieldwork.schemas import (
    CorrectRequest,
    ImportRequest,
    RelationCreate,
    ReviewRequest,
    SealRequest,
    SquareCreate,
    UnitCreate,
    UnitUpdate,
)
from app.fieldwork.service import FieldworkService

router = APIRouter(prefix="/api/projects/{project_id}/fieldwork", tags=["fieldwork"])


@router.post("/squares", status_code=201)
def create_square(project_id: int, payload: SquareCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldworkService().create_square(project_id, payload.model_dump(), user["id"], idempotency_key)


@router.get("/squares")
def list_squares(project_id: int, user=Depends(current_user)):
    return {"data": FieldworkService().list_squares(project_id, user["id"])}


@router.post("/units", status_code=201)
def create_unit(project_id: int, payload: UnitCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldworkService().create_unit(project_id, payload.model_dump(), user["id"], idempotency_key)


@router.get("/units")
def list_units(
    project_id: int,
    square_id: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    status: str | None = Query(default=None),
    user=Depends(current_user),
):
    return {"data": FieldworkService().list_units(project_id, user["id"], square_id, kind, status)}


@router.get("/units/{unit_id}")
def get_unit(project_id: int, unit_id: int, user=Depends(current_user)):
    return FieldworkService().get_unit(project_id, unit_id, user["id"])


@router.patch("/units/{unit_id}")
def update_unit(project_id: int, unit_id: int, payload: UnitUpdate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldworkService().update_unit(project_id, unit_id, payload.model_dump(), user["id"], idempotency_key)


@router.post("/units/{unit_id}/seal")
def seal_unit(project_id: int, unit_id: int, payload: SealRequest, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldworkService().seal_unit(project_id, unit_id, payload.model_dump(), user["id"], idempotency_key)


@router.post("/units/{unit_id}/correct")
def correct_unit(project_id: int, unit_id: int, payload: CorrectRequest, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldworkService().correct_unit(project_id, unit_id, payload.model_dump(), user["id"], idempotency_key)


@router.post("/units/{unit_id}/review")
def review_unit(project_id: int, unit_id: int, payload: ReviewRequest, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldworkService().review_unit(project_id, unit_id, payload.model_dump(), user["id"], idempotency_key)


@router.get("/units/{unit_id}/versions")
def version_history(project_id: int, unit_id: int, user=Depends(current_user)):
    return FieldworkService().version_history(project_id, unit_id, user["id"])


@router.post("/relations", status_code=201)
def create_relation(project_id: int, payload: RelationCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldworkService().create_relation(project_id, payload.model_dump(), user["id"], idempotency_key)


@router.delete("/relations/{relation_id}")
def delete_relation(project_id: int, relation_id: int, user=Depends(current_user)):
    return FieldworkService().delete_relation(project_id, relation_id, user["id"])


@router.get("/units/{unit_id}/relations")
def direct_relations(project_id: int, unit_id: int, user=Depends(current_user)):
    return FieldworkService().direct_relations(project_id, unit_id, user["id"])


@router.get("/units/{unit_id}/relations/transitive")
def transitive_relations(project_id: int, unit_id: int, user=Depends(current_user)):
    return FieldworkService().transitive_relations(project_id, unit_id, user["id"])


@router.post("/import")
def import_batch(project_id: int, payload: ImportRequest, user=Depends(current_user), idempotency_key: str = Header(default="")):
    rows = [row.model_dump(by_alias=True) for row in payload.rows]
    return FieldworkService().import_batch(project_id, rows, user["id"], idempotency_key)
