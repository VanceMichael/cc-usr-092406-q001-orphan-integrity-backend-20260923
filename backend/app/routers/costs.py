from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import CostRecord
from ..schemas import CostRecordCreate, CostRecordUpdate, CostRecordResponse
from .. import integrity as service

router = APIRouter(
    prefix="/api/cost-records",
    tags=["成本核算"]
)

LABEL = "成本记录"
TABLE = "cost_records"


@router.post("/", response_model=CostRecordResponse)
def create_cost_record(record: CostRecordCreate, db: Session = Depends(get_db)):
    return service.create_child(db, CostRecord, record.model_dump())


@router.get("/", response_model=List[CostRecordResponse])
def get_cost_records(skip: int = 0, limit: int = 100, batch_id: int = None, cost_type: str = None, db: Session = Depends(get_db)):
    query = db.query(CostRecord)
    if batch_id:
        query = query.filter(CostRecord.batch_id == batch_id)
    if cost_type:
        query = query.filter(CostRecord.cost_type == cost_type)
    return query.offset(skip).limit(limit).all()


@router.get("/{record_id}/", response_model=CostRecordResponse)
def get_cost_record(record_id: int, db: Session = Depends(get_db)):
    return service.get_child_or_404(db, CostRecord, TABLE, record_id, LABEL)


@router.put("/{record_id}/", response_model=CostRecordResponse)
def update_cost_record(record_id: int, record: CostRecordUpdate, db: Session = Depends(get_db)):
    return service.update_child(
        db, CostRecord, TABLE, record_id, record.model_dump(exclude_unset=True), LABEL
    )


@router.delete("/{record_id}/")
def delete_cost_record(record_id: int, db: Session = Depends(get_db)):
    return service.delete_child(db, CostRecord, TABLE, record_id, LABEL)
