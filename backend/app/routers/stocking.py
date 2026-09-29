from ..models import StockingRecord
from ..schemas import (
    StockingRecordCreate, StockingRecordUpdate, StockingRecordResponse,
)
from ._child_router import build_child_router

router = build_child_router(
    prefix="/api/stocking-records",
    tag="投苗记录",
    model_cls=StockingRecord,
    create_schema=StockingRecordCreate,
    update_schema=StockingRecordUpdate,
    response_schema=StockingRecordResponse,
    not_found_message="投苗记录不存在",
)
