"""统一的父子引用规则。

所有明细（投苗、投喂、水质、用药、成本、销售）在创建、改挂、删除时都走
这里的同一套校验，错误以稳定的业务代码返回；SQLite 的 ON DELETE RESTRICT
与外键强制是兜底，本模块负责把并发与状态差异翻译成明确的冲突。
"""
import json
from datetime import date, datetime

from sqlalchemy import Date, DateTime, func, text
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from .models import (
    Batch,
    StockingRecord,
    FeedingRecord,
    WaterQualityRecord,
    MedicationRecord,
    CostRecord,
    HarvestSale,
    QuarantineRecord,
)

# 稳定错误代码：API 调用方据此区分四种互斥情形。
PARENT_NOT_FOUND = "parent_not_found"        # 父批次不存在
BATCH_CLOSED = "batch_closed"                # 批次已封档
BATCH_DELETING = "batch_deleting"            # 批次正在删除
BATCH_HAS_CHILDREN = "batch_has_children"    # 批次仍有明细，拒绝删除
HISTORICAL_ORPHAN = "historical_orphan"      # 历史孤儿：现存明细的父批次已不存在
RECORD_NOT_FOUND = "record_not_found"

# table -> (ORM 模型, 中文名称)
CHILD_REGISTRY = {
    "stocking_records": (StockingRecord, "投苗记录"),
    "feeding_records": (FeedingRecord, "投喂记录"),
    "water_quality_records": (WaterQualityRecord, "水质记录"),
    "medication_records": (MedicationRecord, "用药记录"),
    "cost_records": (CostRecord, "成本记录"),
    "harvest_sales": (HarvestSale, "销售记录"),
}


class IntegrityConflict(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def _conflict(code, message, status_code=409, **details):
    return IntegrityConflict(status_code, code, message, details)


def get_batch_or_raise(db, batch_id: int) -> Batch:
    """读取父批次并区分：不存在 / 删除中。"""
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise _conflict(
            PARENT_NOT_FOUND, f"批次 {batch_id} 不存在", status_code=404, batch_id=batch_id
        )
    if batch.status == "deleting":
        raise _conflict(
            BATCH_DELETING,
            f"批次 {batch.batch_number} 正在删除，暂不能挂接明细",
            status_code=409,
            batch_id=batch_id,
        )
    return batch


def ensure_writable_batch(db, batch_id: int) -> Batch:
    """创建明细或把明细改挂进来时使用：父批次必须存在、未封档、未在删除。"""
    batch = get_batch_or_raise(db, batch_id)
    if batch.status == "closed":
        raise _conflict(
            BATCH_CLOSED,
            f"批次 {batch.batch_number} 已关闭，不能新增或改挂明细",
            status_code=409,
            batch_id=batch_id,
        )
    return batch


def count_children(db, batch_id: int) -> dict:
    return {
        table: db.query(func.count(model.id)).filter(model.batch_id == batch_id).scalar() or 0
        for table, (model, _label) in CHILD_REGISTRY.items()
    }


def create_child(db, model, values: dict):
    """统一创建入口：先在同一事务内确认父批次可写，再插入。"""
    ensure_writable_batch(db, values["batch_id"])
    row = model(**values)
    db.add(row)
    try:
        db.commit()
    except SAIntegrityError:
        # 数据库外键兜底（并发删除刚刚提交等情形）。
        db.rollback()
        if not db.get(Batch, values["batch_id"]):
            raise _conflict(
                PARENT_NOT_FOUND,
                f"批次 {values['batch_id']} 不存在",
                status_code=404,
                batch_id=values["batch_id"],
            )
        raise
    db.refresh(row)
    return row


def get_child_or_404(db, model, table: str, record_id: int, label: str):
    """取现存明细；若其父批次已消失，明确标记为历史孤儿而不是静默返回。"""
    row = db.get(model, record_id)
    if row is None:
        raise _conflict(
            RECORD_NOT_FOUND, f"{label} {record_id} 不存在", status_code=404, record_id=record_id
        )
    if db.get(Batch, row.batch_id) is None:
        raise _conflict(
            HISTORICAL_ORPHAN,
            f"{label} {record_id} 引用的批次 {row.batch_id} 已不存在，属历史孤儿，请在隔离区复核",
            status_code=410,
            table=table,
            record_id=record_id,
            orphan_batch_id=row.batch_id,
        )
    return row


def update_child(db, model, table: str, record_id: int, values: dict, label: str):
    """统一改挂/修改入口。"""
    row = get_child_or_404(db, model, table, record_id, label)
    target_batch_id = values.get("batch_id", row.batch_id)

    if "batch_id" in values and values["batch_id"] != row.batch_id:
        # 从已封档批次移出同样不允许（会改写封存数据）。
        source = db.get(Batch, row.batch_id)
        if source is not None and source.status == "closed":
            raise _conflict(
                BATCH_CLOSED,
                f"批次 {source.batch_number} 已关闭，其明细不能改挂",
                batch_id=row.batch_id,
            )
        ensure_writable_batch(db, target_batch_id)
    else:
        batch = get_batch_or_raise(db, target_batch_id)
        if batch.status == "closed":
            raise _conflict(
                BATCH_CLOSED,
                f"批次 {batch.batch_number} 已关闭，其明细为只读",
                batch_id=target_batch_id,
            )

    for key, value in values.items():
        setattr(row, key, value)
    try:
        db.commit()
    except SAIntegrityError:
        db.rollback()
        if "batch_id" in values and db.get(Batch, values["batch_id"]) is None:
            raise _conflict(
                PARENT_NOT_FOUND,
                f"批次 {values['batch_id']} 不存在",
                status_code=404,
                batch_id=values["batch_id"],
            )
        raise
    db.refresh(row)
    return row


def delete_child(db, model, table: str, record_id: int, label: str):
    # 删除明细不需要父批次仍在（孤儿行可经隔离区处置）；现存行先做孤儿识别。
    row = db.get(model, record_id)
    if row is None:
        raise _conflict(
            RECORD_NOT_FOUND, f"{label} {record_id} 不存在", status_code=404, record_id=record_id
        )
    db.delete(row)
    db.commit()
    return {"message": f"{label}删除成功", "id": record_id}


def mark_batch_deleting(db, batch_id: int) -> Batch:
    """删除第一阶段：把 deleting 标记独立提交，使其崩溃后仍可核对。"""
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise _conflict(PARENT_NOT_FOUND, "批次不存在", status_code=404, batch_id=batch_id)
    if batch.status == "deleting":
        raise _conflict(
            BATCH_DELETING, "批次正在删除中", status_code=409, batch_id=batch_id
        )
    batch.status = "deleting"
    db.commit()
    return batch


def delete_batch(db, batch_id: int):
    """删除批次：空批次可删；有明细一律稳定 409，不级联、不留孤儿。

    两阶段：先持久化 deleting（与本批次竞争的补录请求据此得到明确的
    “正在删除”冲突码），随后在新事务里复查明细数量——
      - 期间已有补录入账 -> 恢复 active 并返回 batch_has_children；
      - 仍为空 -> 真正删除。
    连接以 BEGIN IMMEDIATE 开启，两个写事务在 SQLite 层排队，
    清理与现场补录只有一方能提交。
    """
    batch = db.get(Batch, batch_id)
    if batch is None:
        raise _conflict(PARENT_NOT_FOUND, "批次不存在", status_code=404, batch_id=batch_id)

    if batch.status != "deleting":
        mark_batch_deleting(db, batch_id)
        db.expire_all()
        batch = db.get(Batch, batch_id)
        if batch is None:
            raise _conflict(PARENT_NOT_FOUND, "批次不存在", status_code=404, batch_id=batch_id)

    counts = count_children(db, batch_id)
    if any(counts.values()):
        # 竞争失败：恢复可用状态，把批次留给管理员，绝不静默删除明细。
        batch.status = "active"
        db.commit()
        raise _conflict(
            BATCH_HAS_CHILDREN,
            f"批次 {batch.batch_number} 仍有明细记录，不能删除",
            status_code=409,
            batch_id=batch_id,
            batch_number=batch.batch_number,
            child_counts=counts,
        )

    db.delete(batch)
    db.commit()
    return {"message": "批次删除成功", "id": batch_id}


# ---------------------------------------------------------------------------
# 隔离区与复核
# ---------------------------------------------------------------------------

def _row_payload(model, row) -> str:
    data = {}
    for column in model.__table__.columns:
        value = getattr(row, column.name)
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        data[column.name] = value
    return json.dumps(data, ensure_ascii=False)


def quarantine_orphan(
    db,
    table: str,
    row,
    reason: str,
    reason_detail: str,
    review_status: str = "pending",
    resolved_batch_id=None,
    commit: bool = True,
):
    """把一条孤儿原始行快照进隔离台账并从原表移除（补正场景记 repaired）。"""
    model, _label = CHILD_REGISTRY[table]
    entry = QuarantineRecord(
        source_table=table,
        source_id=row.id,
        orphan_batch_id=row.batch_id,
        orphan_batch_number=getattr(row, "batch_number", None),
        payload=_row_payload(model, row),
        reason=reason,
        reason_detail=reason_detail,
        review_status=review_status,
        resolved_batch_id=resolved_batch_id,
    )
    db.add(entry)
    db.delete(row)
    if commit:
        db.commit()
        db.refresh(entry)
    return entry


def list_quarantine(db, review_status: str = None):
    query = db.query(QuarantineRecord)
    if review_status:
        query = query.filter(QuarantineRecord.review_status == review_status)
    return query.order_by(QuarantineRecord.id).all()


def resolve_quarantine(db, entry_id: int, action: str, batch_id=None, reviewed_by=None):
    entry = db.get(QuarantineRecord, entry_id)
    if entry is None:
        raise _conflict(RECORD_NOT_FOUND, "隔离记录不存在", status_code=404, entry_id=entry_id)
    if entry.review_status != "pending":
        raise _conflict(
            "quarantine_already_reviewed",
            f"隔离记录已复核：{entry.review_status}",
            status_code=409,
            review_status=entry.review_status,
        )

    if action == "reject":
        # 管理员确认无法找回，原行保持删除状态。
        entry.review_status = "rejected"
    elif action == "repair":
        if batch_id is None:
            raise _conflict(
                "repair_requires_batch",
                "补正必须提供目标批次 batch_id",
                status_code=400,
            )
        target = db.get(Batch, batch_id)
        if target is None:
            raise _conflict(
                PARENT_NOT_FOUND, f"批次 {batch_id} 不存在", status_code=404, batch_id=batch_id
            )
        if target.status == "deleting":
            raise _conflict(BATCH_DELETING, "目标批次正在删除", batch_id=batch_id)
        model, _label = CHILD_REGISTRY[entry.source_table]
        payload = json.loads(entry.payload)
        payload["batch_id"] = batch_id
        # 以全新主键回插，避免与迁移后新分配的 id 冲突。
        payload.pop("id", None)
        # JSON 快照中的日期是 ISO 字符串，按列类型还原。
        for column in model.__table__.columns:
            value = payload.get(column.name)
            if value is None:
                continue
            if isinstance(column.type, DateTime):
                payload[column.name] = datetime.fromisoformat(value)
            elif isinstance(column.type, Date):
                payload[column.name] = date.fromisoformat(value)
        db.add(model(**payload))
        entry.review_status = "repaired"
        entry.resolved_batch_id = batch_id
    else:
        raise _conflict("invalid_action", "action 只能是 repair 或 reject", status_code=400)

    entry.reviewed_by = reviewed_by
    entry.reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(entry)
    return entry


def reconcile_pending_deletions(db) -> list:
    """启动对账：清理崩溃残留在 deleting 中间态的批次。

    删除从未确认提交，按“数据优先”恢复为 active，而不是替用户删除；
    返回被恢复的批次 id 供启动日志核对。
    """
    stuck = db.query(Batch).filter(Batch.status == "deleting").all()
    recovered = [batch.id for batch in stuck]
    for batch in stuck:
        batch.status = "active"
    if recovered:
        db.commit()
    return recovered


def integrity_overview(db):
    """供状态端点与重启核对：约束版本、外键开关、隔离与复核进度。"""
    from .models import SchemaMigration

    pragma = db.execute(text("PRAGMA foreign_keys")).scalar()
    versions = [v[0] for v in db.query(SchemaMigration.version).all()]
    latest = db.query(SchemaMigration).order_by(SchemaMigration.applied_at.desc()).first()

    rows = db.query(
        QuarantineRecord.review_status, func.count(QuarantineRecord.id)
    ).group_by(QuarantineRecord.review_status).all()
    by_status = {status: count for status, count in rows}

    pending_by_table = dict(
        db.query(QuarantineRecord.source_table, func.count(QuarantineRecord.id))
        .filter(QuarantineRecord.review_status == "pending")
        .group_by(QuarantineRecord.source_table)
        .all()
    )

    return {
        "foreign_keys_enforced": bool(pragma),
        "migration_versions": versions,
        "latest_migration": {
            "version": latest.version,
            "applied_at": (
                latest.applied_at.isoformat()
                if hasattr(latest.applied_at, "isoformat")
                else latest.applied_at
            ),
            "detail": json.loads(latest.detail) if latest and latest.detail else None,
        },
        "quarantine": {
            "pending": by_status.get("pending", 0),
            "repaired": by_status.get("repaired", 0),
            "rejected": by_status.get("rejected", 0),
            "pending_by_table": pending_by_table,
        },
    }
