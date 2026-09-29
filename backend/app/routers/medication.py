from ..models import MedicationRecord
from ..schemas import (
    MedicationRecordCreate, MedicationRecordUpdate, MedicationRecordResponse,
)
from ._child_router import build_child_router

router = build_child_router(
    prefix="/api/medication-records",
    tag="用药记录",
    model_cls=MedicationRecord,
    create_schema=MedicationRecordCreate,
    update_schema=MedicationRecordUpdate,
    response_schema=MedicationRecordResponse,
    not_found_message="用药记录不存在",
)
