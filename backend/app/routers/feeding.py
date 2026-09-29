from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import FeedingRecord
from ..schemas import FeedingRecordCreate, FeedingRecordUpdate, FeedingRecordResponse
from .. import integrity as service

router = APIRouter(
    prefix="/api/feeding-records",
    tags=["投喂记录"]
)

LABEL = "投喂记录"
TABLE = "feeding_records"


@router.post("/", response_model=FeedingRecordResponse)
def create_feeding_record(record: FeedingRecordCreate, db: Session = Depends(get_db)):
    return service.create_child(db, FeedingRecord, record.model_dump())


@router.get("/", response_model=List[FeedingRecordResponse])
def get_feeding_records(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(FeedingRecord)
    if batch_id:
        query = query.filter(FeedingRecord.batch_id == batch_id)
    return query.offset(skip).limit(limit).all()


@router.get("/{record_id}/", response_model=FeedingRecordResponse)
def get_feeding_record(record_id: int, db: Session = Depends(get_db)):
    return service.get_child_or_404(db, FeedingRecord, TABLE, record_id, LABEL)


@router.put("/{record_id}/", response_model=FeedingRecordResponse)
def update_feeding_record(record_id: int, record: FeedingRecordUpdate, db: Session = Depends(get_db)):
    return service.update_child(
        db, FeedingRecord, TABLE, record_id, record.model_dump(exclude_unset=True), LABEL
    )


@router.delete("/{record_id}/")
def delete_feeding_record(record_id: int, db: Session = Depends(get_db)):
    return service.delete_child(db, FeedingRecord, TABLE, record_id, LABEL)
