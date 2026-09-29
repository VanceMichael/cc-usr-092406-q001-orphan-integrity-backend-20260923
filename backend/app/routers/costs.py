from ..models import CostRecord
from ..schemas import (
    CostRecordCreate, CostRecordUpdate, CostRecordResponse,
)
from ._child_router import build_child_router

router = build_child_router(
    prefix="/api/cost-records",
    tag="成本核算",
    model_cls=CostRecord,
    create_schema=CostRecordCreate,
    update_schema=CostRecordUpdate,
    response_schema=CostRecordResponse,
    not_found_message="成本记录不存在",
)
