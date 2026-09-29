from ..models import FeedingRecord
from ..schemas import (
    FeedingRecordCreate, FeedingRecordUpdate, FeedingRecordResponse,
)
from ._child_router import build_child_router

router = build_child_router(
    prefix="/api/feeding-records",
    tag="投喂记录",
    model_cls=FeedingRecord,
    create_schema=FeedingRecordCreate,
    update_schema=FeedingRecordUpdate,
    response_schema=FeedingRecordResponse,
    not_found_message="投喂记录不存在",
)
