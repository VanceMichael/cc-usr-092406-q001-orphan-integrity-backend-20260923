from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import Pond, Batch
from ..schemas import PondCreate, PondUpdate, PondResponse
from .. import integrity as service

router = APIRouter(
    prefix="/api/ponds",
    tags=["塘口管理"]
)


@router.post("/", response_model=PondResponse)
def create_pond(pond: PondCreate, db: Session = Depends(get_db)):
    if db.query(Pond).filter(Pond.name == pond.name).first():
        raise service._conflict("pond_name_exists", "塘口名称已存在", status_code=409)
    new_pond = Pond(**pond.model_dump())
    db.add(new_pond)
    db.commit()
    db.refresh(new_pond)
    return new_pond


@router.get("/", response_model=List[PondResponse])
def get_ponds(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return db.query(Pond).offset(skip).limit(limit).all()


@router.get("/{pond_id}/", response_model=PondResponse)
def get_pond(pond_id: int, db: Session = Depends(get_db)):
    pond = db.get(Pond, pond_id)
    if not pond:
        raise service._conflict("pond_not_found", "塘口不存在", status_code=404, pond_id=pond_id)
    return pond


@router.put("/{pond_id}/", response_model=PondResponse)
def update_pond(pond_id: int, pond: PondUpdate, db: Session = Depends(get_db)):
    db_pond = db.get(Pond, pond_id)
    if not db_pond:
        raise service._conflict("pond_not_found", "塘口不存在", status_code=404, pond_id=pond_id)

    update_data = pond.model_dump(exclude_unset=True)
    if "name" in update_data:
        clash = db.query(Pond).filter(
            Pond.name == update_data["name"], Pond.id != pond_id
        ).first()
        if clash:
            raise service._conflict("pond_name_exists", "塘口名称已存在", status_code=409)

    for key, value in update_data.items():
        setattr(db_pond, key, value)
    db.commit()
    db.refresh(db_pond)
    return db_pond


@router.delete("/{pond_id}/")
def delete_pond(pond_id: int, db: Session = Depends(get_db)):
    db_pond = db.get(Pond, pond_id)
    if not db_pond:
        raise service._conflict("pond_not_found", "塘口不存在", status_code=404, pond_id=pond_id)

    # 取得写锁后统计批次：有批次（任何状态）都拒绝删除。
    db_pond.status = db_pond.status or "active"
    db.flush()
    batch_count = db.query(func.count(Batch.id)).filter(Batch.pond_id == pond_id).scalar() or 0
    if batch_count:
        db.rollback()
        raise service._conflict(
            "pond_has_batches",
            f"塘口 {db_pond.name} 仍有 {batch_count} 个批次，不能删除",
            status_code=409,
            pond_id=pond_id,
            batch_count=batch_count,
        )

    db.delete(db_pond)
    db.commit()
    return {"message": "塘口删除成功", "id": pond_id}
