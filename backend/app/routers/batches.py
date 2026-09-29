from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db, mark_write_intent
from ..errors import AppError, batch_not_found
from ..models import Batch, Pond
from ..schemas import BatchCreate, BatchUpdate, BatchResponse
from .. import services

router = APIRouter(
    prefix="/api/batches",
    tags=["批次管理"]
)


@router.post("/", response_model=BatchResponse)
def create_batch(batch: BatchCreate, db: Session = Depends(get_db)):
    mark_write_intent()
    if db.query(Pond).filter(Pond.id == batch.pond_id).first() is None:
        raise AppError(404, "pond_not_found", "塘口不存在",
                       {"pond_id": batch.pond_id})
    if db.query(Batch).filter(Batch.batch_number == batch.batch_number).first():
        raise AppError(400, "batch_number_exists", "批次号已存在",
                       {"batch_number": batch.batch_number})

    new_batch = Batch(**batch.model_dump())
    db.add(new_batch)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(400, "batch_number_exists", "批次号已存在",
                       {"batch_number": batch.batch_number})
    db.refresh(new_batch)
    return new_batch


@router.get("/", response_model=List[BatchResponse])
def get_batches(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return db.query(Batch).offset(skip).limit(limit).all()


@router.get("/{batch_id}/", response_model=BatchResponse)
def get_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not batch:
        raise batch_not_found(batch_id)
    return batch


@router.get("/by-number/{batch_number}/", response_model=BatchResponse)
def get_batch_by_number(batch_number: str, db: Session = Depends(get_db)):
    batch = db.query(Batch).filter(Batch.batch_number == batch_number).first()
    if not batch:
        raise AppError(404, "batch_not_found",
                       f"批次号 {batch_number} 不存在",
                       {"batch_number": batch_number})
    return batch


@router.put("/{batch_id}/", response_model=BatchResponse)
def update_batch(batch_id: int, batch: BatchUpdate, db: Session = Depends(get_db)):
    mark_write_intent()
    db_batch = db.query(Batch).filter(Batch.id == batch_id).first()
    if not db_batch:
        raise batch_not_found(batch_id)

    data = batch.model_dump(exclude_unset=True)
    # 手工把 deleting 改回正常状态不允许：删除流程独占该状态。
    if db_batch.status == "deleting" and data.get("status") != "deleting":
        raise AppError(409, "batch_deleting",
                       "批次正在删除，暂不能修改", {"batch_id": batch_id})
    if "pond_id" in data:
        if db.query(Pond).filter(Pond.id == data["pond_id"]).first() is None:
            raise AppError(404, "pond_not_found", "塘口不存在",
                           {"pond_id": data["pond_id"]})
    for key, value in data.items():
        setattr(db_batch, key, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(400, "batch_number_exists", "批次号已存在")
    db.refresh(db_batch)
    return db_batch


@router.delete("/{batch_id}/")
def delete_batch(batch_id: int, db: Session = Depends(get_db)):
    # 两阶段删除：有明细稳定 409 冲突，无级联、无孤儿。
    return services.delete_batch(db, batch_id)
