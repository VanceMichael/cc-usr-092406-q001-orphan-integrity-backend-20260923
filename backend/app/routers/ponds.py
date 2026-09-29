from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db, mark_write_intent
from ..errors import AppError
from ..models import Pond
from ..schemas import PondCreate, PondUpdate, PondResponse
from .. import services

router = APIRouter(
    prefix="/api/ponds",
    tags=["塘口管理"]
)


@router.post("/", response_model=PondResponse)
def create_pond(pond: PondCreate, db: Session = Depends(get_db)):
    mark_write_intent()
    if db.query(Pond).filter(Pond.name == pond.name).first():
        raise AppError(400, "pond_name_exists", "塘口名称已存在",
                       {"name": pond.name})
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
    pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not pond:
        raise AppError(404, "pond_not_found", "塘口不存在", {"pond_id": pond_id})
    return pond


@router.put("/{pond_id}/", response_model=PondResponse)
def update_pond(pond_id: int, pond: PondUpdate, db: Session = Depends(get_db)):
    mark_write_intent()
    db_pond = db.query(Pond).filter(Pond.id == pond_id).first()
    if not db_pond:
        raise AppError(404, "pond_not_found", "塘口不存在", {"pond_id": pond_id})

    for key, value in pond.model_dump(exclude_unset=True).items():
        setattr(db_pond, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(400, "pond_name_exists", "塘口名称已存在")
    db.refresh(db_pond)
    return db_pond


@router.delete("/{pond_id}/")
def delete_pond(pond_id: int, db: Session = Depends(get_db)):
    # 同一父子规则：塘口下仍有批次时稳定冲突，不级联。
    db_pond = services.require_pond_deletable(db, pond_id)
    db.delete(db_pond)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "pond_has_batches",
                       "塘口下仍有养殖批次，拒绝删除", {"pond_id": pond_id})
    return {"message": "塘口删除成功"}
