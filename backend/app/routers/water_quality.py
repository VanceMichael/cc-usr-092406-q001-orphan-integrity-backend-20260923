from ..models import WaterQualityRecord
from ..schemas import (
    WaterQualityRecordCreate, WaterQualityRecordUpdate,
    WaterQualityRecordResponse,
)
from ._child_router import build_child_router

router = build_child_router(
    prefix="/api/water-quality-records",
    tag="水质监测",
    model_cls=WaterQualityRecord,
    create_schema=WaterQualityRecordCreate,
    update_schema=WaterQualityRecordUpdate,
    response_schema=WaterQualityRecordResponse,
    not_found_message="水质监测记录不存在",
)
