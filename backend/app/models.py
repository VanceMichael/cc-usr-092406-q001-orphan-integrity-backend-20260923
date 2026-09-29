from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base

# 父子记录类型 -> 表名，供迁移与隔离复核共用
CHILD_TABLES = {
    "stocking": "stocking_records",
    "feeding": "feeding_records",
    "water_quality": "water_quality_records",
    "medication": "medication_records",
    "cost": "cost_records",
    "harvest_sale": "harvest_sales",
}


class Pond(Base):
    __tablename__ = "ponds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True, nullable=False)
    area = Column(Float, nullable=False, comment="面积(亩)")
    water_depth = Column(Float, nullable=False, comment="水深(米)")
    species = Column(String(100), comment="养殖品种")
    status = Column(String(20), default="active", comment="状态: active, inactive")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batches = relationship("Batch", back_populates="pond")


class Batch(Base):
    __tablename__ = "batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_number = Column(String(50), unique=True, index=True, nullable=False, comment="批次号")
    pond_id = Column(Integer, ForeignKey("ponds.id", ondelete="RESTRICT"),
                     nullable=False)
    species = Column(String(100), nullable=False, comment="养殖品种")
    stocking_date = Column(Date, nullable=False, comment="放苗日期")
    estimated_harvest_date = Column(Date, comment="预计收获日期")
    actual_harvest_date = Column(Date, comment="实际收获日期")
    status = Column(String(20), default="active",
                    comment="状态: active, harvested, closed, deleting")
    pre_delete_status = Column(String(20), comment="两阶段删除前的原状态，冲突时恢复")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    pond = relationship("Pond", back_populates="batches")
    stocking_records = relationship("StockingRecord", back_populates="batch", passive_deletes=True)
    feeding_records = relationship("FeedingRecord", back_populates="batch", passive_deletes=True)
    water_quality_records = relationship("WaterQualityRecord", back_populates="batch", passive_deletes=True)
    medication_records = relationship("MedicationRecord", back_populates="batch", passive_deletes=True)
    cost_records = relationship("CostRecord", back_populates="batch", passive_deletes=True)
    harvest_sales = relationship("HarvestSale", back_populates="batch", passive_deletes=True)


def _batch_fk():
    """明细记录指向批次的通用外键：数据库层 RESTRICT，杜绝直写/并发产生孤儿。"""
    return Column(Integer, ForeignKey("batches.id", ondelete="RESTRICT"), nullable=False)


class StockingRecord(Base):
    __tablename__ = "stocking_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = _batch_fk()
    species = Column(String(100), nullable=False, comment="品种")
    quantity = Column(Integer, nullable=False, comment="数量(尾)")
    source = Column(String(200), comment="来源")
    batch_number = Column(String(50), comment="苗种批次号")
    weight_per_unit = Column(Float, comment="单重(克/尾)")
    total_weight = Column(Float, comment="总重量(公斤)")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="stocking_records")


class FeedingRecord(Base):
    __tablename__ = "feeding_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = _batch_fk()
    feeding_date = Column(Date, nullable=False, comment="投喂日期")
    feed_type = Column(String(100), nullable=False, comment="饲料类型")
    feed_quantity = Column(Float, nullable=False, comment="投喂量(公斤)")
    feeding_time = Column(String(20), comment="投喂时间")
    weather = Column(String(50), comment="天气情况")
    water_temperature = Column(Float, comment="水温(℃)")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="feeding_records")


class WaterQualityRecord(Base):
    __tablename__ = "water_quality_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = _batch_fk()
    record_date = Column(Date, nullable=False, comment="检测日期")
    record_time = Column(String(20), comment="检测时间")
    water_temperature = Column(Float, comment="水温(℃)")
    ph_value = Column(Float, comment="pH值")
    dissolved_oxygen = Column(Float, comment="溶解氧(mg/L)")
    ammonia_nitrogen = Column(Float, comment="氨氮(mg/L)")
    nitrite = Column(Float, comment="亚硝酸盐(mg/L)")
    transparency = Column(Float, comment="透明度(cm)")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="water_quality_records")


class MedicationRecord(Base):
    __tablename__ = "medication_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = _batch_fk()
    medication_date = Column(Date, nullable=False, comment="用药日期")
    drug_name = Column(String(200), nullable=False, comment="药品名称")
    drug_type = Column(String(50), comment="药品类型")
    dosage = Column(Float, comment="用量")
    dosage_unit = Column(String(20), default="kg", comment="用量单位")
    administration_method = Column(String(100), comment="施用方法")
    purpose = Column(String(200), comment="用途")
    manufacturer = Column(String(200), comment="生产厂家")
    batch_number = Column(String(50), comment="药品批次号")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="medication_records")


class CostRecord(Base):
    __tablename__ = "cost_records"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = _batch_fk()
    cost_date = Column(Date, nullable=False, comment="费用日期")
    cost_type = Column(String(50), nullable=False,
                       comment="费用类型: feed, medicine, labor, electricity, other")
    amount = Column(Float, nullable=False, comment="金额(元)")
    description = Column(String(500), comment="费用描述")
    quantity = Column(Float, comment="数量")
    unit = Column(String(20), comment="单位")
    unit_price = Column(Float, comment="单价")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="cost_records")


class HarvestSale(Base):
    __tablename__ = "harvest_sales"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = _batch_fk()
    sale_date = Column(Date, nullable=False, comment="销售日期")
    weight = Column(Float, nullable=False, comment="重量(公斤)")
    unit_price = Column(Float, nullable=False, comment="单价(元/公斤)")
    total_amount = Column(Float, comment="总金额(元)")
    buyer = Column(String(200), comment="买家")
    batch_number = Column(String(50), comment="追溯批次号")
    quality_grade = Column(String(50), comment="质量等级")
    notes = Column(Text, comment="备注")
    created_at = Column(DateTime, default=datetime.utcnow)

    batch = relationship("Batch", back_populates="harvest_sales")


class OrphanQuarantine(Base):
    """迁移中无法唯一归属的历史孤儿：迁出原表、保留快照与原因，等待人工复核。"""

    __tablename__ = "orphan_quarantine"

    id = Column(Integer, primary_key=True, index=True)
    record_type = Column(String(30), nullable=False, index=True, comment="记录类型")
    source_table = Column(String(60), nullable=False, comment="原表名")
    source_id = Column(Integer, nullable=False, comment="原记录主键")
    missing_batch_ref = Column(Integer, comment="失效的 batch_id")
    batch_number_hint = Column(String(50), comment="记录上残留的批次号文本")
    reason = Column(String(100), nullable=False, comment="隔离原因")
    payload_json = Column(Text, nullable=False, comment="原始行完整 JSON 快照")
    status = Column(String(20), default="pending", nullable=False, index=True,
                    comment="复核状态: pending, resolved(补挂), discarded(作废)")
    resolved_batch_id = Column(Integer, comment="复核后补挂的批次")
    resolution_note = Column(String(500), comment="复核说明")
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, comment="复核时间")


class MigrationLog(Base):
    """迁移/启动恢复进度，服务重启后仍可核对。"""

    __tablename__ = "migration_log"

    id = Column(Integer, primary_key=True, index=True)
    from_version = Column(Integer, nullable=False)
    to_version = Column(Integer, nullable=False)
    event = Column(String(30), nullable=False, comment="migrate / startup-recover")
    detail_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=datetime.utcnow)
