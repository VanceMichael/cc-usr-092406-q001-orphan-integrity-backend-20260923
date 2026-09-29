from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from .. import integrity as service

router = APIRouter(prefix="/api/integrity", tags=["引用完整性"])


class QuarantineResponse(BaseModel):
    id: int
    source_table: str
    source_id: int
    orphan_batch_id: Optional[int] = None
    orphan_batch_number: Optional[str] = None
    payload: str
    reason: str
    reason_detail: Optional[str] = None
    review_status: str
    reviewed_by: Optional[str] = None
    resolved_batch_id: Optional[int] = None

    class Config:
        from_attributes = True


class ReviewRequest(BaseModel):
    action: str  # repair | reject
    batch_id: Optional[int] = None
    reviewed_by: Optional[str] = None


@router.get("/status/")
def integrity_status(db: Session = Depends(get_db)):
    """约束、迁移版本与隔离复核进度总览，重启后仍可核对。"""
    return service.integrity_overview(db)


@router.get("/quarantine/", response_model=List[QuarantineResponse])
def list_quarantine(review_status: Optional[str] = None, db: Session = Depends(get_db)):
    return service.list_quarantine(db, review_status)


@router.post("/quarantine/{entry_id}/review/", response_model=QuarantineResponse)
def review_quarantine(entry_id: int, body: ReviewRequest, db: Session = Depends(get_db)):
    return service.resolve_quarantine(
        db,
        entry_id,
        action=body.action,
        batch_id=body.batch_id,
        reviewed_by=body.reviewed_by,
    )
