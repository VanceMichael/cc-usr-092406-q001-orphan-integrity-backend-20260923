from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import WaterQualityRecord
from ..schemas import WaterQualityRecordCreate, WaterQualityRecordUpdate, WaterQualityRecordResponse
from .. import integrity as service

router = APIRouter(
    prefix="/api/water-quality-records",
    tags=["水质监测"]
)

LABEL = "水质记录"
TABLE = "water_quality_records"


@router.post("/", response_model=WaterQualityRecordResponse)
def create_water_quality_record(record: WaterQualityRecordCreate, db: Session = Depends(get_db)):
    return service.create_child(db, WaterQualityRecord, record.model_dump())


@router.get("/", response_model=List[WaterQualityRecordResponse])
def get_water_quality_records(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(WaterQualityRecord)
    if batch_id:
        query = query.filter(WaterQualityRecord.batch_id == batch_id)
    return query.offset(skip).limit(limit).all()


@router.get("/{record_id}/", response_model=WaterQualityRecordResponse)
def get_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    return service.get_child_or_404(db, WaterQualityRecord, TABLE, record_id, LABEL)


@router.put("/{record_id}/", response_model=WaterQualityRecordResponse)
def update_water_quality_record(record_id: int, record: WaterQualityRecordUpdate, db: Session = Depends(get_db)):
    return service.update_child(
        db, WaterQualityRecord, TABLE, record_id, record.model_dump(exclude_unset=True), LABEL
    )


@router.delete("/{record_id}/")
def delete_water_quality_record(record_id: int, db: Session = Depends(get_db)):
    return service.delete_child(db, WaterQualityRecord, TABLE, record_id, LABEL)
