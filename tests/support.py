"""测试共用支撑：独立临时库、旧库构造、依赖注入覆盖与并发竞速。"""

import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# 在导入应用前把全局库指到临时目录，避免在仓库里生成 aquaculture.db。
_TMP = Path(tempfile.mkdtemp(prefix="aqua-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'global.db'}")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import (  # noqa: E402
    Base, build_sqlite_engine, get_db, mark_write_intent,
)
from app.migration import (  # noqa: E402
    migrate_database, ensure_integrity_triggers_for_engine,
)


def fresh_engine(db_path: Path):
    """为指定文件跑迁移、建表、装触发器，返回 (engine, SessionLocal)。"""
    migrate_database(str(db_path))
    eng = build_sqlite_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=eng)
    ensure_integrity_triggers_for_engine(eng)
    from sqlalchemy.orm import sessionmaker
    return eng, sessionmaker(autocommit=False, autoflush=False, bind=eng)


def make_client(db_path: Path):
    """让整套 API 跑在独立临时库上。"""
    eng, Session = fresh_engine(db_path)

    def _get_db():
        db = Session()
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    client = TestClient(app)
    return client, eng, Session


def new_db_file(name: str) -> Path:
    p = _TMP / name
    if p.exists():
        p.unlink()
    for suffix in ("-wal", "-shm"):
        sp = Path(str(p) + suffix)
        if sp.exists():
            sp.unlink()
    return p


# ---------------------------------------------------------------- 旧库构造

# 旧版（user_version=0）建表语句：外键未强制、无 pre_delete_status。
LEGACY_DDL = """
CREATE TABLE ponds (
    id INTEGER PRIMARY KEY, name VARCHAR(100) UNIQUE, area FLOAT,
    water_depth FLOAT, species VARCHAR(100), status VARCHAR(20) DEFAULT 'active',
    created_at DATETIME, updated_at DATETIME
);
CREATE TABLE batches (
    id INTEGER PRIMARY KEY, batch_number VARCHAR(50) UNIQUE, pond_id INTEGER,
    species VARCHAR(100), stocking_date DATE, estimated_harvest_date DATE,
    actual_harvest_date DATE, status VARCHAR(20) DEFAULT 'active',
    created_at DATETIME, updated_at DATETIME
);
CREATE TABLE stocking_records (
    id INTEGER PRIMARY KEY, batch_id INTEGER, species VARCHAR(100),
    quantity INTEGER, source VARCHAR(200), batch_number VARCHAR(50),
    weight_per_unit FLOAT, total_weight FLOAT, notes TEXT, created_at DATETIME
);
CREATE TABLE feeding_records (
    id INTEGER PRIMARY KEY, batch_id INTEGER, feeding_date DATE,
    feed_type VARCHAR(100), feed_quantity FLOAT, feeding_time VARCHAR(20),
    weather VARCHAR(50), water_temperature FLOAT, notes TEXT, created_at DATETIME
);
CREATE TABLE water_quality_records (
    id INTEGER PRIMARY KEY, batch_id INTEGER, record_date DATE,
    record_time VARCHAR(20), water_temperature FLOAT, ph_value FLOAT,
    dissolved_oxygen FLOAT, ammonia_nitrogen FLOAT, nitrite FLOAT,
    transparency FLOAT, notes TEXT, created_at DATETIME
);
CREATE TABLE medication_records (
    id INTEGER PRIMARY KEY, batch_id INTEGER, medication_date DATE,
    drug_name VARCHAR(200), drug_type VARCHAR(50), dosage FLOAT,
    dosage_unit VARCHAR(20), administration_method VARCHAR(100),
    purpose VARCHAR(200), manufacturer VARCHAR(200), batch_number VARCHAR(50),
    notes TEXT, created_at DATETIME
);
CREATE TABLE cost_records (
    id INTEGER PRIMARY KEY, batch_id INTEGER, cost_date DATE,
    cost_type VARCHAR(50), amount FLOAT, description VARCHAR(500),
    quantity FLOAT, unit VARCHAR(20), unit_price FLOAT, notes TEXT,
    created_at DATETIME
);
CREATE TABLE harvest_sales (
    id INTEGER PRIMARY KEY, batch_id INTEGER, sale_date DATE, weight FLOAT,
    unit_price FLOAT, total_amount FLOAT, buyer VARCHAR(200),
    batch_number VARCHAR(50), quality_grade VARCHAR(50), notes TEXT,
    created_at DATETIME
);
"""


def build_legacy_db(db_path: Path, extra_sql: str = ""):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(LEGACY_DDL)
        conn.executescript(extra_sql)
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ 并发

def run_concurrent(*targets, timeout: int = 30):
    """同屏障同时起跑多个线程，返回 {name: 结果或异常}。"""
    barrier = threading.Barrier(len(targets))
    results = {}

    def _wrap(name, fn):
        def runner():
            barrier.wait()
            try:
                results[name] = ("ok", fn())
            except Exception as exc:  # noqa: BLE001 - 测试里需要捕获全部结局
                results[name] = ("error", f"{type(exc).__name__}:{exc}")
        return runner

    threads = [threading.Thread(target=_wrap(n, f), name=n)
               for n, f in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
        assert not t.is_alive(), f"线程 {t.name} 超时"
    return results


def raw_connect(db_path: Path, foreign_keys: bool = False):
    """模拟外部直连（默认不开启 foreign_keys pragma）。"""
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
    return conn
