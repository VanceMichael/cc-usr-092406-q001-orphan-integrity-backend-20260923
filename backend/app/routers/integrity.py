import json
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from .. import services

router = APIRouter(
    prefix="/api/integrity",
    tags=["引用完整性"]
)


class QuarantineResponse(BaseModel):
    id: int
    record_type: str
    source_table: str
    source_id: int
    missing_batch_ref: Optional[int] = None
    batch_number_hint: Optional[str] = None
    reason: str
    payload: dict
    status: str
    resolved_batch_id: Optional[int] = None
    resolution_note: Optional[str] = None

    class Config:
        from_attributes = True


class QuarantineResolveRequest(BaseModel):
    action: str  # reattach / discard
    target_batch_id: Optional[int] = None
    note: Optional[str] = None


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """约束、隔离结果与复核进度总览，重启后仍可核对。"""
    return services.integrity_status(db)


@router.get("/migration-history")
def migration_history(db: Session = Depends(get_db)):
    return {"events": services.migration_history(db)}


@router.get("/quarantine/", response_model=List[QuarantineResponse])
def list_quarantine(status_filter: str = None, record_type: str = None,
                    db: Session = Depends(get_db)):
    items = services.list_quarantine(db, status_filter, record_type)
    result = []
    for item in items:
        result.append({
            "id": item.id,
            "record_type": item.record_type,
            "source_table": item.source_table,
            "source_id": item.source_id,
            "missing_batch_ref": item.missing_batch_ref,
            "batch_number_hint": item.batch_number_hint,
            "reason": item.reason,
            "payload": json.loads(item.payload_json),
            "status": item.status,
            "resolved_batch_id": item.resolved_batch_id,
            "resolution_note": item.resolution_note,
        })
    return result


@router.post("/quarantine/{quarantine_id}/resolve")
def resolve_quarantine(quarantine_id: int,
                       body: QuarantineResolveRequest,
                       db: Session = Depends(get_db)):
    """人工复核：reattach=补挂到指定批次，discard=确认作废。结果持久化。"""
    return services.resolve_quarantine(
        db, quarantine_id, body.action,
        body.target_batch_id, body.note)
