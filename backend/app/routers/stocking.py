from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import StockingRecord
from ..schemas import StockingRecordCreate, StockingRecordUpdate, StockingRecordResponse
from .. import integrity as service

router = APIRouter(
    prefix="/api/stocking-records",
    tags=["投苗记录"]
)

LABEL = "投苗记录"
TABLE = "stocking_records"


@router.post("/", response_model=StockingRecordResponse)
def create_stocking_record(record: StockingRecordCreate, db: Session = Depends(get_db)):
    return service.create_child(db, StockingRecord, record.model_dump())


@router.get("/", response_model=List[StockingRecordResponse])
def get_stocking_records(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(StockingRecord)
    if batch_id:
        query = query.filter(StockingRecord.batch_id == batch_id)
    return query.offset(skip).limit(limit).all()


@router.get("/{record_id}/", response_model=StockingRecordResponse)
def get_stocking_record(record_id: int, db: Session = Depends(get_db)):
    return service.get_child_or_404(db, StockingRecord, TABLE, record_id, LABEL)


@router.put("/{record_id}/", response_model=StockingRecordResponse)
def update_stocking_record(record_id: int, record: StockingRecordUpdate, db: Session = Depends(get_db)):
    return service.update_child(
        db, StockingRecord, TABLE, record_id, record.model_dump(exclude_unset=True), LABEL
    )


@router.delete("/{record_id}/")
def delete_stocking_record(record_id: int, db: Session = Depends(get_db)):
    return service.delete_child(db, StockingRecord, TABLE, record_id, LABEL)
