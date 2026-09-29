from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import Batch, Pond
from ..schemas import BatchCreate, BatchUpdate, BatchResponse
from .. import integrity as service

router = APIRouter(
    prefix="/api/batches",
    tags=["批次管理"]
)

ALLOWED_BATCH_STATUS = {"active", "closed"}


@router.post("/", response_model=BatchResponse)
def create_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    if batch.status is not None and batch.status not in ALLOWED_BATCH_STATUS:
        raise service._conflict(
            "invalid_batch_status",
            f"批次状态只能是 {sorted(ALLOWED_BATCH_STATUS)}",
            status_code=400,
        )
    if db.get(Pond, batch.pond_id) is None:
        raise service._conflict(
            service.PARENT_NOT_FOUND, "塘口不存在", status_code=404, pond_id=batch.pond_id
        )
    existing = db.query(Batch).filter(Batch.batch_number == batch.batch_number).first()
    if existing:
        raise service._conflict(
            "batch_number_exists", "批次号已存在", status_code=409,
            batch_number=batch.batch_number,
        )

    new_batch = Batch(**batch.model_dump())
    db.add(new_batch)
    db.commit()
    db.refresh(new_batch)
    return new_batch


@router.get("/", response_model=List[BatchResponse])
def get_batches(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return db.query(Batch).offset(skip).limit(limit).all()


@router.get("/{batch_id}/", response_model=BatchResponse)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise service._conflict(
            service.PARENT_NOT_FOUND, "批次不存在", status_code=404, batch_id=batch_id
        )
    return batch


@router.get("/by-number/{batch_number}/", response_model=BatchResponse)
def get_batch_by_number(batch_number: str, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.batch_number == batch_number).first()
    if not batch:
        raise service._conflict(
            service.PARENT_NOT_FOUND,
            f"批次 {batch_number} 不存在",
            status_code=404,
            batch_number=batch_number,
        )
    return batch


@router.put("/{batch_id}/", response_model=BatchResponse)
def update_batch(batch_id: int, batch: BatchUpdate, db: Session = Depends(get_db)):
    db_batch = db.get(Batch, batch_id)
    if not db_batch:
        raise service._conflict(
            service.PARENT_NOT_FOUND, "批次不存在", status_code=404, batch_id=batch_id
        )

    update_data = batch.model_dump(exclude_unset=True)
    if "status" in update_data and update_data["status"] not in ALLOWED_BATCH_STATUS:
        raise service._conflict(
            "invalid_batch_status",
            f"批次状态只能是 {sorted(ALLOWED_BATCH_STATUS)}；deleting 为系统内部状态",
            status_code=400,
        )
    if "pond_id" in update_data and db.get(Pond, update_data["pond_id"]) is None:
        raise service._conflict(
            service.PARENT_NOT_FOUND,
            "塘口不存在",
            status_code=404,
            pond_id=update_data["pond_id"],
        )

    for key, value in update_data.items():
        setattr(db_batch, key, value)
    db.commit()
    db.refresh(db_batch)
    return db_batch


@router.delete("/{batch_id}/")
def delete_batch(batch_id: int, db: Session = Depends(get_db)):
    # 有明细的批次返回稳定冲突；空批次删除，绝不级联。
    return service.delete_batch(db, batch_id)
