"""六类明细路由的统一工厂：创建/查询/改挂/删除全部走 services 同一父子规则。"""

from typing import List, Type

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from .. import services


def build_child_router(*, prefix: str, tag: str, model_cls, create_schema,
                       update_schema, response_schema, not_found_message: str):
    router = APIRouter(prefix=prefix, tags=[tag])

    @router.post("/", response_model=response_schema)
    def create_record(record: create_schema, db: Session = Depends(get_db)):
        return services.create_child(db, model_cls, record.model_dump())

    @router.get("/", response_model=List[response_schema])
    def list_records(skip: int = 0, limit: int = 100, batch_id: int = None,
                     db: Session = Depends(get_db)):
        query = db.query(model_cls)
        if batch_id:
            query = query.filter(model_cls.batch_id == batch_id)
        return query.offset(skip).limit(limit).all()

    @router.get("/{record_id}/", response_model=response_schema)
    def get_record(record_id: int, db: Session = Depends(get_db)):
        record = db.query(model_cls).filter(model_cls.id == record_id).first()
        if record is None:
            from ..errors import AppError
            raise AppError(404, "record_not_found", not_found_message,
                           {"record_id": record_id})
        return record

    @router.put("/{record_id}/", response_model=response_schema)
    def update_record(record_id: int, record: update_schema,
                      db: Session = Depends(get_db)):
        return services.update_child(
            db, model_cls, record_id, record.model_dump(exclude_unset=True),
            not_found_message)

    @router.delete("/{record_id}/")
    def remove_record(record_id: int, db: Session = Depends(get_db)):
        return services.delete_child(db, model_cls, record_id, not_found_message)

    return router
