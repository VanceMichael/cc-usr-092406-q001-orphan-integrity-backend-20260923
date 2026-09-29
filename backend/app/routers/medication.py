from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import MedicationRecord
from ..schemas import MedicationRecordCreate, MedicationRecordUpdate, MedicationRecordResponse
from .. import integrity as service

router = APIRouter(
    prefix="/api/medication-records",
    tags=["用药记录"]
)

LABEL = "用药记录"
TABLE = "medication_records"


@router.post("/", response_model=MedicationRecordResponse)
def create_medication_record(record: MedicationRecordCreate, db: Session = Depends(get_db)):
    return service.create_child(db, MedicationRecord, record.model_dump())


@router.get("/", response_model=List[MedicationRecordResponse])
def get_medication_records(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(MedicationRecord)
    if batch_id:
        query = query.filter(MedicationRecord.batch_id == batch_id)
    return query.offset(skip).limit(limit).all()


@router.get("/{record_id}/", response_model=MedicationRecordResponse)
def get_medication_record(record_id: int, db: Session = Depends(get_db)):
    return service.get_child_or_404(db, MedicationRecord, TABLE, record_id, LABEL)


@router.put("/{record_id}/", response_model=MedicationRecordResponse)
def update_medication_record(record_id: int, record: MedicationRecordUpdate, db: Session = Depends(get_db)):
    return service.update_child(
        db, MedicationRecord, TABLE, record_id, record.model_dump(exclude_unset=True), LABEL
    )


@router.delete("/{record_id}/")
def delete_medication_record(record_id: int, db: Session = Depends(get_db)):
    return service.delete_child(db, MedicationRecord, TABLE, record_id, LABEL)
