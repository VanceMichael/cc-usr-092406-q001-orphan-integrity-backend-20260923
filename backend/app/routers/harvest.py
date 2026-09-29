from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from ..database import get_db
from ..models import HarvestSale
from ..schemas import HarvestSaleCreate, HarvestSaleUpdate, HarvestSaleResponse
from .. import integrity as service

router = APIRouter(
    prefix="/api/harvest-sales",
    tags=["出塘销售"]
)

LABEL = "销售记录"
TABLE = "harvest_sales"


@router.post("/", response_model=HarvestSaleResponse)
def create_harvest_sale(sale: HarvestSaleCreate, db: Session = Depends(get_db)):
    values = sale.model_dump()
    if values.get("total_amount") is None:
        values["total_amount"] = values["weight"] * values["unit_price"]
    return service.create_child(db, HarvestSale, values)


@router.get("/", response_model=List[HarvestSaleResponse])
def get_harvest_sales(skip: int = 0, limit: int = 100, batch_id: int = None, db: Session = Depends(get_db)):
    query = db.query(HarvestSale)
    if batch_id:
        query = query.filter(HarvestSale.batch_id == batch_id)
    return query.offset(skip).limit(limit).all()


@router.get("/{sale_id}/", response_model=HarvestSaleResponse)
def get_harvest_sale(sale_id: int, db: Session = Depends(get_db)):
    return service.get_child_or_404(db, HarvestSale, TABLE, sale_id, LABEL)


@router.put("/{sale_id}/", response_model=HarvestSaleResponse)
def update_harvest_sale(sale_id: int, sale: HarvestSaleUpdate, db: Session = Depends(get_db)):
    values = sale.model_dump(exclude_unset=True)
    if "weight" in values or "unit_price" in values:
        existing = service.get_child_or_404(db, HarvestSale, TABLE, sale_id, LABEL)
        weight = values.get("weight", existing.weight)
        unit_price = values.get("unit_price", existing.unit_price)
        values["total_amount"] = weight * unit_price
    return service.update_child(db, HarvestSale, TABLE, sale_id, values, LABEL)


@router.delete("/{sale_id}/")
def delete_harvest_sale(sale_id: int, db: Session = Depends(get_db)):
    return service.delete_child(db, HarvestSale, TABLE, sale_id, LABEL)
