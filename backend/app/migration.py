"""旧库升级与启动恢复。

版本约定（PRAGMA user_version）：
  0 - 历史库：明细外键未强制、可留孤儿
  1 - 全部父子外键以 ON DELETE RESTRICT 落地、孤儿已补正或隔离

v0 -> v1 做的事：
  1. 扫描六类明细中 batch_id 已失效的孤儿；
  2. 能凭记录上的批次号文本唯一归属到现存批次的，安全补正 batch_id；
  3. 无法判断（无批次号 / 批次号查无 / 命中多个）的，整行快照迁入
     orphan_quarantine 隔离并保留原因，等待人工复核；
  4. 按 v1 结构重建明细表与批次表，装上 RESTRICT 外键（SQLite 无法
     直接 ALTER 外键，只能重建）；
  5. 恢复上次进程崩溃时残留在 deleting 状态的批次。

迁移器只使用原生 sqlite3，不导入 ORM，避免与应用引擎互相递归。
"""

import json
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 1

# 明细表：列名按建表顺序排列，重建时按列拷贝。
# hint_column 为记录上留存的批次号文本（仅部分表有），用于唯一归属判定。
CHILD_SPECS = [
    {
        "type": "stocking",
        "table": "stocking_records",
        "hint_column": "batch_number",
        "columns": [
            ("id", "INTEGER PRIMARY KEY"),
            ("batch_id", "INTEGER NOT NULL REFERENCES batches(id) ON DELETE RESTRICT"),
            ("species", "VARCHAR(100) NOT NULL"),
            ("quantity", "INTEGER NOT NULL"),
            ("source", "VARCHAR(200)"),
            ("batch_number", "VARCHAR(50)"),
            ("weight_per_unit", "FLOAT"),
            ("total_weight", "FLOAT"),
            ("notes", "TEXT"),
            ("created_at", "DATETIME"),
        ],
        "indexes": ["id", "batch_id"],
    },
    {
        "type": "feeding",
        "table": "feeding_records",
        "hint_column": None,
        "columns": [
            ("id", "INTEGER PRIMARY KEY"),
            ("batch_id", "INTEGER NOT NULL REFERENCES batches(id) ON DELETE RESTRICT"),
            ("feeding_date", "DATE NOT NULL"),
            ("feed_type", "VARCHAR(100) NOT NULL"),
            ("feed_quantity", "FLOAT NOT NULL"),
            ("feeding_time", "VARCHAR(20)"),
            ("weather", "VARCHAR(50)"),
            ("water_temperature", "FLOAT"),
            ("notes", "TEXT"),
            ("created_at", "DATETIME"),
        ],
        "indexes": ["id", "batch_id"],
    },
    {
        "type": "water_quality",
        "table": "water_quality_records",
        "hint_column": None,
        "columns": [
            ("id", "INTEGER PRIMARY KEY"),
            ("batch_id", "INTEGER NOT NULL REFERENCES batches(id) ON DELETE RESTRICT"),
            ("record_date", "DATE NOT NULL"),
            ("record_time", "VARCHAR(20)"),
            ("water_temperature", "FLOAT"),
            ("ph_value", "FLOAT"),
            ("dissolved_oxygen", "FLOAT"),
            ("ammonia_nitrogen", "FLOAT"),
            ("nitrite", "FLOAT"),
            ("transparency", "FLOAT"),
            ("notes", "TEXT"),
            ("created_at", "DATETIME"),
        ],
        "indexes": ["id", "batch_id"],
    },
    {
        "type": "medication",
        "table": "medication_records",
        "hint_column": "batch_number",
        "columns": [
            ("id", "INTEGER PRIMARY KEY"),
            ("batch_id", "INTEGER NOT NULL REFERENCES batches(id) ON DELETE RESTRICT"),
            ("medication_date", "DATE NOT NULL"),
            ("drug_name", "VARCHAR(200) NOT NULL"),
            ("drug_type", "VARCHAR(50)"),
            ("dosage", "FLOAT"),
            ("dosage_unit", "VARCHAR(20) DEFAULT 'kg'"),
            ("administration_method", "VARCHAR(100)"),
            ("purpose", "VARCHAR(200)"),
            ("manufacturer", "VARCHAR(200)"),
            ("batch_number", "VARCHAR(50)"),
            ("notes", "TEXT"),
            ("created_at", "DATETIME"),
        ],
        "indexes": ["id", "batch_id"],
    },
    {
        "type": "cost",
        "table": "cost_records",
        "hint_column": None,
        "columns": [
            ("id", "INTEGER PRIMARY KEY"),
            ("batch_id", "INTEGER NOT NULL REFERENCES batches(id) ON DELETE RESTRICT"),
            ("cost_date", "DATE NOT NULL"),
            ("cost_type", "VARCHAR(50) NOT NULL"),
            ("amount", "FLOAT NOT NULL"),
            ("description", "VARCHAR(500)"),
            ("quantity", "FLOAT"),
            ("unit", "VARCHAR(20)"),
            ("unit_price", "FLOAT"),
            ("notes", "TEXT"),
            ("created_at", "DATETIME"),
        ],
        "indexes": ["id", "batch_id"],
    },
    {
        "type": "harvest_sale",
        "table": "harvest_sales",
        "hint_column": "batch_number",
        "columns": [
            ("id", "INTEGER PRIMARY KEY"),
            ("batch_id", "INTEGER NOT NULL REFERENCES batches(id) ON DELETE RESTRICT"),
            ("sale_date", "DATE NOT NULL"),
            ("weight", "FLOAT NOT NULL"),
            ("unit_price", "FLOAT NOT NULL"),
            ("total_amount", "FLOAT"),
            ("buyer", "VARCHAR(200)"),
            ("batch_number", "VARCHAR(50)"),
            ("quality_grade", "VARCHAR(50)"),
            ("notes", "TEXT"),
            ("created_at", "DATETIME"),
        ],
        "indexes": ["id", "batch_id"],
    },
]

BATCHES_V1_DDL = """
CREATE TABLE batches_v1 (
    id INTEGER PRIMARY KEY,
    batch_number VARCHAR(50) NOT NULL UNIQUE,
    pond_id INTEGER NOT NULL REFERENCES ponds(id) ON DELETE RESTRICT,
    species VARCHAR(100) NOT NULL,
    stocking_date DATE NOT NULL,
    estimated_harvest_date DATE,
    actual_harvest_date DATE,
    status VARCHAR(20) DEFAULT 'active',
    pre_delete_status VARCHAR(20),
    created_at DATETIME,
    updated_at DATETIME
)
"""

QUARANTINE_DDL = """
CREATE TABLE IF NOT EXISTS orphan_quarantine (
    id INTEGER PRIMARY KEY,
    record_type VARCHAR(30) NOT NULL,
    source_table VARCHAR(60) NOT NULL,
    source_id INTEGER NOT NULL,
    missing_batch_ref INTEGER,
    batch_number_hint VARCHAR(50),
    reason VARCHAR(100) NOT NULL,
    payload_json TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    resolved_batch_id INTEGER,
    resolution_note VARCHAR(500),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    resolved_at DATETIME
)
"""

MIGRATION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS migration_log (
    id INTEGER PRIMARY KEY,
    from_version INTEGER NOT NULL,
    to_version INTEGER NOT NULL,
    event VARCHAR(30) NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

# 触发器不受 PRAGMA foreign_keys 影响：即使外部直连（pragma 默认关闭）
# 也无法插入/改挂出孤儿，也无法删除仍有明细的批次或仍有批次的塘口。
def child_block_ddl(table: str) -> list:
    return [
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{table}_no_orphan_insert
        BEFORE INSERT ON {table}
        WHEN (SELECT id FROM batches WHERE id = NEW.batch_id) IS NULL
        BEGIN
            SELECT RAISE(ABORT, '{table}.batch_id 引用了不存在的批次');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_{table}_no_orphan_update
        BEFORE UPDATE OF batch_id ON {table}
        WHEN (SELECT id FROM batches WHERE id = NEW.batch_id) IS NULL
        BEGIN
            SELECT RAISE(ABORT, '{table}.batch_id 改挂到了不存在的批次');
        END
        """,
    ]


BATCHES_DELETE_GUARD = """
CREATE TRIGGER IF NOT EXISTS trg_batches_block_delete_with_children
BEFORE DELETE ON batches
WHEN
    EXISTS (SELECT 1 FROM stocking_records WHERE batch_id = OLD.id)
    OR EXISTS (SELECT 1 FROM feeding_records WHERE batch_id = OLD.id)
    OR EXISTS (SELECT 1 FROM water_quality_records WHERE batch_id = OLD.id)
    OR EXISTS (SELECT 1 FROM medication_records WHERE batch_id = OLD.id)
    OR EXISTS (SELECT 1 FROM cost_records WHERE batch_id = OLD.id)
    OR EXISTS (SELECT 1 FROM harvest_sales WHERE batch_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, '批次下仍有明细，禁止删除');
END
"""

PONDS_DELETE_GUARD = """
CREATE TRIGGER IF NOT EXISTS trg_ponds_block_delete_with_batches
BEFORE DELETE ON ponds
WHEN EXISTS (SELECT 1 FROM batches WHERE pond_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, '塘口下仍有批次，禁止删除');
END
"""


def _existing_tables(conn) -> set:
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def integrity_trigger_statements(existing: set) -> list:
    """按实际存在的表生成直写防线触发器 SQL（兼容缺表的旧库）。"""
    stmts = []
    child_tables = []
    for spec in CHILD_SPECS:
        table = spec["table"]
        if table in existing:
            child_tables.append(table)
            stmts.extend(child_block_ddl(table))
    if "batches" in existing and child_tables:
        guards = " OR ".join(
            f"EXISTS (SELECT 1 FROM {t} WHERE batch_id = OLD.id)"
            for t in child_tables)
        stmts.append(
            "CREATE TRIGGER IF NOT EXISTS trg_batches_block_delete_with_children "
            "BEFORE DELETE ON batches WHEN " + guards +
            " BEGIN SELECT RAISE(ABORT, '批次下仍有明细，禁止删除'); END")
    if "ponds" in existing and "batches" in existing:
        stmts.append(PONDS_DELETE_GUARD)
    return stmts


def ensure_integrity_triggers(conn):
    for stmt in integrity_trigger_statements(_existing_tables(conn)):
        conn.execute(stmt)


def ensure_integrity_triggers_for_engine(engine):
    """经业务引擎幂等安装触发器：覆盖全新库（create_all 后）与
    已是 v1 但早期版本未建触发器的库。触发器随库文件持久化。"""
    from sqlalchemy import text
    with engine.begin() as conn:
        existing = {
            r[0] for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        for stmt in integrity_trigger_statements(existing):
            conn.execute(text(stmt))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _write_log(conn, from_version, to_version, event, detail):
    conn.execute(
        "INSERT INTO migration_log (from_version, to_version, event, detail_json) "
        "VALUES (?, ?, ?, ?)",
        (from_version, to_version, event, json.dumps(detail, ensure_ascii=False)),
    )


def _recover_deleting_batches(conn) -> int:
    """两阶段删除在阶段一崩溃时会留下 status=deleting 的批次，重启恢复为 active。"""
    if not _table_exists(conn, "batches"):
        return 0
    cur = conn.execute(
        "UPDATE batches SET status='active', pre_delete_status=NULL "
        "WHERE status='deleting'"
    )
    return cur.rowcount


def _attribution(conn, hint):
    """按批次号文本找唯一归属。返回 (batch_id 或 None, 原因)。"""
    if not hint:
        return None, "no_batch_number_hint"
    rows = conn.execute(
        "SELECT id FROM batches WHERE batch_number=?", (hint,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0], None
    if len(rows) == 0:
        return None, "batch_number_not_found"
    return None, "ambiguous_batch_number"


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> dict:
    conn.execute(QUARANTINE_DDL)
    conn.execute(MIGRATION_LOG_DDL)

    detail = {"repaired": {}, "quarantined": {}}

    # 1) 孤儿扫描：先安全补正，再隔离无法判断的。
    for spec in CHILD_SPECS:
        table = spec["table"]
        if not _table_exists(conn, table):
            continue
        orphans = conn.execute(
            f"SELECT * FROM {table} WHERE batch_id NOT IN "
            f"(SELECT id FROM batches)"
        ).fetchall()
        col_names = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]

        repaired = 0
        quarantined = 0
        for row in orphans:
            data = dict(zip(col_names, row))
            hint = data.get(spec["hint_column"]) if spec["hint_column"] else None
            target_id, reason = _attribution(conn, hint)
            if target_id is not None:
                conn.execute(
                    f"UPDATE {table} SET batch_id=? WHERE id=?",
                    (target_id, data["id"]),
                )
                repaired += 1
                continue

            conn.execute(
                "INSERT INTO orphan_quarantine "
                "(record_type, source_table, source_id, missing_batch_ref, "
                " batch_number_hint, reason, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    spec["type"], table, data["id"], data.get("batch_id"),
                    hint, reason, json.dumps(data, ensure_ascii=False, default=str),
                ),
            )
            conn.execute(f"DELETE FROM {table} WHERE id=?", (data["id"],))
            quarantined += 1

        detail["repaired"][spec["type"]] = repaired
        detail["quarantined"][spec["type"]] = quarantined

    # 2) 重建明细表，装 ON DELETE RESTRICT 外键（孤儿此时已清空）。
    for spec in CHILD_SPECS:
        table = spec["table"]
        if not _table_exists(conn, table):
            continue
        col_names = [c[0] for c in spec["columns"]]
        ddl_body = ", ".join(f"{name} {decl}" for name, decl in spec["columns"])
        conn.execute(f"CREATE TABLE {table}_v1 ({ddl_body})")
        placeholders = ", ".join(col_names)
        conn.execute(
            f"INSERT INTO {table}_v1 ({placeholders}) "
            f"SELECT {placeholders} FROM {table}"
        )
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {table}_v1 RENAME TO {table}")
        for col in spec["indexes"]:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_{col} ON {table} ({col})"
            )

    # 3) 重建批次表，装 pond 侧 RESTRICT，顺带恢复 deleting。
    if _table_exists(conn, "batches"):
        conn.execute(BATCHES_V1_DDL)
        conn.execute(
            "INSERT INTO batches_v1 ("
            "id, batch_number, pond_id, species, stocking_date, "
            "estimated_harvest_date, actual_harvest_date, "
            "status, pre_delete_status, created_at, updated_at) "
            "SELECT id, batch_number, pond_id, species, stocking_date, "
            "estimated_harvest_date, actual_harvest_date, "
            "CASE WHEN status='deleting' THEN 'active' ELSE status END, "
            "NULL, created_at, updated_at FROM batches"
        )
        conn.execute("DROP TABLE batches")
        conn.execute("ALTER TABLE batches_v1 RENAME TO batches")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_batches_batch_number "
            "ON batches (batch_number)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_batches_id ON batches (id)")

    # deleting 状态已在批次重建时归一为 active。
    detail["recovered_deleting"] = 0

    # 表重建会清掉触发器，最后统一安装直写防线。
    ensure_integrity_triggers(conn)

    conn.execute("PRAGMA user_version=1")
    _write_log(conn, 0, 1, "migrate", detail)
    return detail


def migrate_database(db_path: str) -> dict:
    """对外入口：升级到当前版本，并执行每次启动都需要的崩溃恢复。"""
    conn = sqlite3.connect(db_path)
    try:
        # 迁移期间允许无 FK 状态搬运历史行；业务引擎连接时另行强制开启。
        conn.execute("PRAGMA foreign_keys=OFF")
        version = conn.execute("PRAGMA user_version").fetchone()[0]

        migrated = None
        if version == 0:
            # 全新空文件：不建表，交给 SQLAlchemy metadata，只标记版本。
            if not _table_exists(conn, "batches"):
                conn.execute(QUARANTINE_DDL)
                conn.execute(MIGRATION_LOG_DDL)
                conn.execute("PRAGMA user_version=1")
                _write_log(conn, 0, 1, "migrate", {"fresh": True})
            else:
                migrated = _migrate_v0_to_v1(conn)

        # 每次启动都恢复残留 deleting（v1 崩溃场景），并记录可核对日志。
        recovered = _recover_deleting_batches(conn)
        if recovered:
            conn.execute(QUARANTINE_DDL)
            conn.execute(MIGRATION_LOG_DDL)
            _write_log(
                conn, SCHEMA_VERSION, SCHEMA_VERSION, "startup-recover",
                {"recovered_deleting_batches": recovered},
            )
        conn.commit()
        return {"migrated": migrated, "recovered_deleting": recovered}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
