"""引用完整性统一父子规则。

所有路由只调用本模块，不在接口里各自检查父记录：
  - 创建/改挂明细：父批次必须存在、未关闭、未在删除；
  - 删除明细：直接删除（子记录可自由删除）；
  - 删除批次：两阶段（deleting 标记 -> 复查子女 -> 提交），有明细稳定冲突；
  - 数据库 RESTRICT 外键是最后防线，直写库/并发交错也无法留下孤儿。
"""

import json
from datetime import date, datetime

from sqlalchemy import Date, DateTime, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .database import mark_write_intent
from .errors import (
    AppError, batch_not_found, batch_closed, batch_deleting,
)

# 六类明细：模型 + 中文名（冲突时返回各类计数）
CHILD_MODELS = [
    (models.StockingRecord, "投苗"),
    (models.FeedingRecord, "投喂"),
    (models.WaterQualityRecord, "水质"),
    (models.MedicationRecord, "用药"),
    (models.CostRecord, "成本"),
    (models.HarvestSale, "销售"),
]

# 记录类型 -> ORM 模型（隔离复核补挂用）
MODEL_BY_TYPE = {
    "stocking": models.StockingRecord,
    "feeding": models.FeedingRecord,
    "water_quality": models.WaterQualityRecord,
    "medication": models.MedicationRecord,
    "cost": models.CostRecord,
    "harvest_sale": models.HarvestSale,
}


def get_batch(db: Session, batch_id: int):
    return db.query(models.Batch).filter(models.Batch.id == batch_id).first()


def require_batch_for_link(db: Session, batch_id: int):
    """创建/改挂明细前的统一父记录检查。"""
    batch = get_batch(db, batch_id)
    if batch is None:
        raise batch_not_found(batch_id)
    if batch.status == "deleting":
        raise batch_deleting(batch)
    if batch.status == "closed":
        raise batch_closed(batch)
    return batch


def _is_fk_violation(exc: IntegrityError) -> bool:
    return "FOREIGN KEY constraint failed" in str(getattr(exc, "orig", exc))


def commit_or_parent_missing(db: Session, batch_id: int):
    """提交；若 RESTRICT 外键在并发下失败，转成稳定的父记录不存在错误。"""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_fk_violation(exc) and get_batch(db, batch_id) is None:
            raise batch_not_found(batch_id)
        # 删除方在此期间把批次置为 deleting 也不允许挂入。
        batch = get_batch(db, batch_id)
        if batch is not None and batch.status == "deleting":
            raise batch_deleting(batch)
        raise
    except Exception:
        db.rollback()
        raise


def create_child(db: Session, model_cls, values: dict):
    mark_write_intent()
    batch_id = values["batch_id"]
    require_batch_for_link(db, batch_id)
    record = model_cls(**values)
    db.add(record)
    commit_or_parent_missing(db, batch_id)
    db.refresh(record)
    return record


def update_child(db: Session, model_cls, record_id: int, values: dict,
                 not_found_message: str):
    mark_write_intent()
    record = db.query(model_cls).filter(model_cls.id == record_id).first()
    if record is None:
        raise AppError(404, "record_not_found", not_found_message,
                       {"record_id": record_id})

    new_batch_id = values.get("batch_id")
    if new_batch_id is not None and new_batch_id != record.batch_id:
        require_batch_for_link(db, new_batch_id)

    for key, value in values.items():
        setattr(record, key, value)

    commit_or_parent_missing(db, new_batch_id or record.batch_id)
    db.refresh(record)
    return record


def delete_child(db: Session, model_cls, record_id: int, not_found_message: str):
    mark_write_intent()
    record = db.query(model_cls).filter(model_cls.id == record_id).first()
    if record is None:
        raise AppError(404, "record_not_found", not_found_message,
                       {"record_id": record_id})
    db.delete(record)
    db.commit()
    return {"message": "删除成功"}


def children_counts(db: Session, batch_id: int) -> dict:
    counts = {}
    for model, label in CHILD_MODELS:
        n = db.query(model).filter(model.batch_id == batch_id).count()
        if n:
            counts[label] = n
    return counts


def delete_batch(db: Session, batch_id: int):
    """两阶段删除：阶段一置 deleting 并提交（崩溃后重启可恢复），
    阶段二持锁复查子女，有明细则回滚标记并稳定冲突，无明细才真正删除。"""
    mark_write_intent()
    batch = get_batch(db, batch_id)
    if batch is None:
        raise batch_not_found(batch_id)
    if batch.status == "deleting":
        raise batch_deleting(batch)

    # 阶段一：公告删除意图。BEGIN IMMEDIATE 保证与并发补录互斥。
    previous_status = batch.status
    batch.pre_delete_status = previous_status
    batch.status = "deleting"
    db.commit()

    # 阶段二：新事务持锁复查（前一事务的锁已释放，负方此刻才能进入）。
    batch = get_batch(db, batch_id)
    if batch is None:
        raise batch_not_found(batch_id)

    counts = children_counts(db, batch_id)
    if counts:
        # 有明细：恢复原状态，稳定冲突，绝不级联、不留删除半成品。
        batch.status = batch.pre_delete_status or "active"
        batch.pre_delete_status = None
        db.commit()
        from .errors import batch_has_children
        raise batch_has_children(batch_id, counts)

    db.delete(batch)
    try:
        db.commit()
    except IntegrityError:
        # 极端并发下补录在阶段二锁前提交：RESTRICT 拦截，恢复批次。
        db.rollback()
        batch = get_batch(db, batch_id)
        if batch is not None:
            batch.status = batch.pre_delete_status or "active"
            batch.pre_delete_status = None
            db.commit()
        from .errors import batch_has_children
        raise batch_has_children(batch_id, children_counts(db, batch_id))

    return {"message": "批次删除成功"}


def require_pond_deletable(db: Session, pond_id: int):
    mark_write_intent()
    pond = db.query(models.Pond).filter(models.Pond.id == pond_id).first()
    if pond is None:
        raise AppError(404, "pond_not_found", "塘口不存在", {"pond_id": pond_id})
    n = db.query(models.Batch).filter(models.Batch.pond_id == pond_id).count()
    if n:
        raise AppError(409, "pond_has_batches",
                       "塘口下仍有养殖批次，拒绝删除",
                       {"pond_id": pond_id, "batch_count": n})
    return pond


# ---------------------------------------------------------------- 隔离复核

def list_quarantine(db: Session, status: str = None, record_type: str = None):
    q = db.query(models.OrphanQuarantine)
    if status:
        q = q.filter(models.OrphanQuarantine.status == status)
    if record_type:
        q = q.filter(models.OrphanQuarantine.record_type == record_type)
    return q.order_by(models.OrphanQuarantine.id).all()


def _resolve_quarantine_row(db: Session, item, action, target_batch_id, note):
    if item.status != "pending":
        raise AppError(409, "quarantine_already_resolved",
                       "该隔离记录已复核", {"quarantine_id": item.id,
                                        "status": item.status})

    if action == "discard":
        item.status = "discarded"
        item.resolution_note = note
        item.resolved_at = datetime.utcnow()
        db.commit()
        return {"id": item.id, "status": "discarded"}

    if action != "reattach":
        raise AppError(400, "invalid_action",
                       "action 必须是 reattach 或 discard", {"action": action})

    target = get_batch(db, target_batch_id)
    if target is None:
        raise batch_not_found(target_batch_id)
    if target.status == "deleting":
        raise batch_deleting(target)

    payload = json.loads(item.payload_json)
    payload.pop("id", None)  # 原主键可能已被复用，以新主键补挂
    payload["batch_id"] = target_batch_id
    model_cls = MODEL_BY_TYPE.get(item.record_type)
    if model_cls is None:
        raise AppError(400, "unknown_record_type",
                       f"未知的隔离记录类型: {item.record_type}",
                       {"record_type": item.record_type})

    # 快照经 JSON 往返后日期变成字符串，按列类型还原为 date/datetime。
    for key, column in model_cls.__table__.columns.items():
        if key not in payload or payload[key] is None or not isinstance(
                payload[key], str):
            continue
        if isinstance(column.type, DateTime):
            payload[key] = datetime.fromisoformat(payload[key])
        elif isinstance(column.type, Date):
            payload[key] = date.fromisoformat(payload[key])

    restored = model_cls(**payload)
    db.add(restored)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "reattach_constraint_failed",
                       "补挂失败：数据不满足当前约束", {"quarantine_id": item.id})
    db.refresh(restored)

    item.status = "resolved"
    item.resolved_batch_id = target_batch_id
    item.resolution_note = note
    item.resolved_at = datetime.utcnow()
    db.commit()
    return {"id": item.id, "status": "resolved",
            "new_record_id": restored.id,
            "resolved_batch_id": target_batch_id}


def resolve_quarantine(db: Session, quarantine_id: int, action: str,
                       target_batch_id: int = None, note: str = None):
    item = db.query(models.OrphanQuarantine).filter(
        models.OrphanQuarantine.id == quarantine_id
    ).first()
    if item is None:
        raise AppError(404, "quarantine_not_found",
                       "隔离记录不存在", {"quarantine_id": quarantine_id})
    mark_write_intent()
    return _resolve_quarantine_row(
        db, item, action, target_batch_id, note)


def integrity_status(db: Session):
    """约束与复核进度总览，供重启后核对。"""
    def scalar(sql):
        return db.execute(text(sql)).scalar()

    fk_on = scalar("PRAGMA foreign_keys")
    version = scalar("PRAGMA user_version")
    journal = scalar("PRAGMA journal_mode")

    pending = db.query(models.OrphanQuarantine).filter(
        models.OrphanQuarantine.status == "pending").count()
    resolved = db.query(models.OrphanQuarantine).filter(
        models.OrphanQuarantine.status == "resolved").count()
    discarded = db.query(models.OrphanQuarantine).filter(
        models.OrphanQuarantine.status == "discarded").count()
    deleting = db.query(models.Batch).filter(
        models.Batch.status == "deleting").count()

    orphan_counts = {}
    for model, label in CHILD_MODELS:
        orphan_counts[label] = db.query(model).filter(
            ~db.query(models.Batch).filter(models.Batch.id == model.batch_id).exists()
        ).count()

    return {
        "schema_version": version,
        "foreign_keys_enforced": bool(fk_on),
        "journal_mode": journal,
        "deleting_batches": deleting,
        "quarantine": {
            "pending": pending,
            "resolved": resolved,
            "discarded": discarded,
        },
        "live_orphan_records": orphan_counts,
    }


def migration_history(db: Session, limit: int = 50):
    rows = db.query(models.MigrationLog).order_by(
        models.MigrationLog.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "from_version": r.from_version,
            "to_version": r.to_version,
            "event": r.event,
            "detail": json.loads(r.detail_json or "{}"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in reversed(rows)
    ]
