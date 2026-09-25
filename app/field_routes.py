"""田野上下文模块路由：登记/封存/纠错/关系复核/查询/批量导入。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.field_schemas import CorrectionCreate, ImportBatch, RelationCreate, RelationReview, UnitCreate, UnitUpdate
from app.field_service import FieldService
from app.service import ResearchService

router = APIRouter(prefix="/api/projects/{project_id}/field", tags=["field"])


def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 会话")
    return ResearchService().authenticate(authorization[7:])


@router.post("/units", status_code=201)
def create_unit(project_id: int, payload: UnitCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldService().create_unit(project_id, user["id"], payload.model_dump(), idempotency_key)


@router.get("/units")
def list_units(project_id: int, user=Depends(current_user)):
    return FieldService().list_units(project_id, user["id"])


@router.get("/units/{code}")
def get_unit(project_id: int, code: str, user=Depends(current_user)):
    return FieldService().get_unit(project_id, user["id"], code)


@router.patch("/units/{code}")
def update_unit(project_id: int, code: str, payload: UnitUpdate, user=Depends(current_user)):
    return FieldService().update_unit(project_id, user["id"], code, payload.model_dump(exclude_unset=True))


@router.post("/units/{code}/seal", status_code=201)
def seal_unit(project_id: int, code: str, user=Depends(current_user)):
    return FieldService().seal_unit(project_id, user["id"], code)


@router.post("/units/{code}/corrections", status_code=201)
def correct_unit(project_id: int, code: str, payload: CorrectionCreate, user=Depends(current_user)):
    return FieldService().correct_unit(project_id, user["id"], code, payload.model_dump(exclude_unset=False))


@router.get("/units/{code}/versions")
def version_history(project_id: int, code: str, user=Depends(current_user)):
    return FieldService().version_history(project_id, user["id"], code)


@router.get("/units/{code}/audit")
def unit_audit(project_id: int, code: str, user=Depends(current_user)):
    return FieldService().unit_audit(project_id, user["id"], code)


@router.get("/units/{code}/relations")
def direct_relations(project_id: int, code: str, status_filter: str | None = Query(default=None, alias="status"), user=Depends(current_user)):
    return FieldService().direct_relations(project_id, user["id"], code, status_filter)


@router.get("/units/{code}/transitive")
def transitive_relations(project_id: int, code: str, user=Depends(current_user)):
    return FieldService().transitive_relations(project_id, user["id"], code)


@router.post("/relations", status_code=201)
def create_relation(project_id: int, payload: RelationCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldService().create_relation(project_id, user["id"], payload.model_dump(), idempotency_key)


@router.get("/relations")
def list_relations(project_id: int, status_filter: str | None = Query(default=None, alias="status"), kind: str | None = Query(default=None), user=Depends(current_user)):
    return FieldService().list_relations(project_id, user["id"], status_filter, kind)


@router.post("/relations/{relation_id}/review")
def review_relation(project_id: int, relation_id: int, payload: RelationReview, user=Depends(current_user)):
    return FieldService().review_relation(project_id, user["id"], relation_id, payload.model_dump())


@router.post("/import")
def import_batch(project_id: int, payload: ImportBatch, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return FieldService().import_batch(project_id, user["id"], payload.model_dump(), idempotency_key)
