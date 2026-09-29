"""旧库升级：补齐 ON DELETE RESTRICT 约束并处置历史孤儿。

整个迁移在一个 BEGIN IMMEDIATE 事务内原子完成（SQLite 允许事务内 DDL）：
中途崩溃则整体回滚，重启重跑；schema_migrations 落账后视为完成，重复启动
直接跳过，因此升级幂等、可核对。

孤儿处置规则（保守、可追溯）：
- 明细行携带的批次号能在现存批次中唯一命中 -> 同事务安全补正 batch_id，
  并在隔离台账留 repaired 记录；
- 批次号缺失或在批次表中查无此号 -> 原始行 JSON 快照入隔离区（pending），
  从明细表移除，保留原因，等待人工复核。
"""
import json
import sqlite3
from datetime import datetime

from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex, CreateTable

from .models import Base, CHILD_TABLES

MIGRATION_VERSION = "2_referential_integrity"

# 需要补齐 ON DELETE RESTRICT 的旧表（ponds 无结构变化，不动）。
REBUILD_TABLES = CHILD_TABLES + ["batches"]

# 携带业务批次号文本、可作为归属线索的明细表。
NUMBER_HINT_TABLES = {
    "stocking_records": "batch_number",
    "medication_records": "batch_number",
    "harvest_sales": "batch_number",
}

REASON_REPAIRED = "repaired_by_batch_number"
REASON_NUMBER_NOT_FOUND = "batch_number_not_found"
REASON_NO_HINT = "no_batch_number_hint"


# 触发器独立于 PRAGMA foreign_keys：外部程序用默认关闭外键的直连写库时，
# ON DELETE RESTRICT 不生效，但 BEFORE 触发器仍会拦截——杜绝直写绕过。
def _trigger_sql():
    statements = []
    for table_name in CHILD_TABLES:
        statements.append(f"DROP TRIGGER IF EXISTS trg_{table_name}_insert_parent")
        statements.append(
            f"""
            CREATE TRIGGER trg_{table_name}_insert_parent
            BEFORE INSERT ON {table_name}
            WHEN NEW.batch_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM batches WHERE id = NEW.batch_id)
            BEGIN
                SELECT RAISE(ABORT, '{table_name}.batch_id 引用的批次不存在');
            END
            """
        )
        statements.append(f"DROP TRIGGER IF EXISTS trg_{table_name}_update_parent")
        statements.append(
            f"""
            CREATE TRIGGER trg_{table_name}_update_parent
            BEFORE UPDATE OF batch_id ON {table_name}
            WHEN NEW.batch_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM batches WHERE id = NEW.batch_id)
            BEGIN
                SELECT RAISE(ABORT, '{table_name}.batch_id 改挂到不存在的批次');
            END
            """
        )
        statements.append(f"DROP TRIGGER IF EXISTS trg_batches_delete_{table_name}")
        statements.append(
            f"""
            CREATE TRIGGER trg_batches_delete_{table_name}
            BEFORE DELETE ON batches
            WHEN EXISTS (SELECT 1 FROM {table_name} WHERE batch_id = OLD.id)
            BEGIN
                SELECT RAISE(ABORT, '批次仍被 {table_name} 引用，禁止删除');
            END
            """
        )
    statements.append("DROP TRIGGER IF EXISTS trg_ponds_delete_batches")
    statements.append(
        """
        CREATE TRIGGER trg_ponds_delete_batches
        BEFORE DELETE ON ponds
        WHEN EXISTS (SELECT 1 FROM batches WHERE pond_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, '塘口仍被 batches 引用，禁止删除');
        END
        """
    )
    return statements


def ensure_triggers(engine=None):
    """幂等创建引用完整性触发器；全新库与旧库迁移后都调用。"""
    from .database import engine as default_engine

    engine = engine or default_engine
    connection = engine.raw_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("BEGIN IMMEDIATE")
        for statement in _trigger_sql():
            cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def _table_ddl(table, temp_name):
    statement = str(
        CreateTable(table).compile(dialect=sqlite_dialect.dialect())
    )
    return statement.replace(f"CREATE TABLE {table.name}", f"CREATE TABLE {temp_name}", 1)


def _index_ddls(table, temp_name):
    dialect = sqlite_dialect.dialect()
    statements = []
    for index in table.indexes:
        ddl = str(CreateIndex(index).compile(dialect=dialect))
        ddl = ddl.replace(index.name, f"_migr_{index.name}", 1)
        ddl = ddl.replace(f"ON {table.name}", f"ON {temp_name}", 1)
        statements.append(ddl)
    return statements


def _needs_rebuild(cursor, table_name) -> bool:
    """现存表的建表 SQL 不含 ON DELETE RESTRICT 即视为旧表。"""
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if row is None:
        return False
    return "ON DELETE RESTRICT" not in (row[0] or "").upper()


def _canonical_index_ddls(table):
    dialect = sqlite_dialect.dialect()
    return [
        str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes
    ]


def _rebuild_table(cursor, table):
    """SQLite 改外键的标准 12 步法（外层已关 foreign_keys 并开事务）。"""
    temp_name = f"_migr_{table.name}"
    columns = [column.name for column in table.columns]
    column_list = ", ".join(columns)

    cursor.execute(f"DROP TABLE IF EXISTS {temp_name}")
    cursor.execute(_table_ddl(table, temp_name))
    for ddl in _index_ddls(table, temp_name):
        cursor.execute(ddl)
    cursor.execute(
        f"INSERT INTO {temp_name} ({column_list}) SELECT {column_list} FROM {table.name}"
    )
    cursor.execute(f"DROP TABLE {table.name}")
    cursor.execute(f"ALTER TABLE {temp_name} RENAME TO {table.name}")
    # 临时索引随表保留但带 _migr_ 前缀，换成与全新库一致的规范索引名。
    for index in table.indexes:
        cursor.execute(f"DROP INDEX IF EXISTS _migr_{index.name}")
    for ddl in _canonical_index_ddls(table):
        cursor.execute(ddl)


def _ensure_ledger_tables(cursor):
    for table_name in ("quarantine_records", "schema_migrations"):
        table = Base.metadata.tables[table_name]
        cursor.execute(
            str(
                CreateTable(table, if_not_exists=True).compile(
                    dialect=sqlite_dialect.dialect()
                )
            )
        )
        for index in table.indexes:
            cursor.execute(
                str(CreateIndex(index, if_not_exists=True).compile(
                    dialect=sqlite_dialect.dialect()))
            )


def _scan_orphans(cursor):
    summary = {}
    for table_name in CHILD_TABLES:
        orphan_rows = cursor.execute(
            f"""
            SELECT * FROM {table_name}
            WHERE batch_id NOT IN (SELECT id FROM batches)
               OR batch_id IS NULL
            """
        ).fetchall()
        columns = [item[0] for item in cursor.description]
        repaired = quarantined = 0

        for orphan in orphan_rows:
            data = dict(zip(columns, orphan))
            hint_column = NUMBER_HINT_TABLES.get(table_name)
            hint_value = data.get(hint_column) if hint_column else None

            target_id = None
            if hint_value:
                match = cursor.execute(
                    "SELECT id FROM batches WHERE batch_number = ?", (hint_value,)
                ).fetchone()
                # batch_number 有 UNIQUE 约束，命中即唯一归属。
                if match is not None:
                    target_id = match[0]

            if target_id is not None:
                cursor.execute(
                    f"UPDATE {table_name} SET batch_id = ? WHERE id = ?",
                    (target_id, data["id"]),
                )
                cursor.execute(
                    """
                    INSERT INTO quarantine_records
                        (source_table, source_id, orphan_batch_id, orphan_batch_number,
                         payload, reason, reason_detail, review_status, resolved_batch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'repaired', ?)
                    """,
                    (
                        table_name,
                        data["id"],
                        data["batch_id"],
                        hint_value,
                        json.dumps(data, ensure_ascii=False),
                        REASON_REPAIRED,
                        f"按唯一批次号 {hint_value} 补正到批次 {target_id}",
                        target_id,
                    ),
                )
                repaired += 1
            else:
                if hint_value:
                    reason = REASON_NUMBER_NOT_FOUND
                    detail = f"明细携带批次号 {hint_value}，但批次表中无此批次，无法唯一归属"
                else:
                    reason = REASON_NO_HINT
                    detail = "明细未携带批次号，原 batch_id 已无对应批次，无法判断归属"
                cursor.execute(
                    """
                    INSERT INTO quarantine_records
                        (source_table, source_id, orphan_batch_id, orphan_batch_number,
                         payload, reason, reason_detail, review_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        table_name,
                        data["id"],
                        data["batch_id"],
                        hint_value,
                        json.dumps(data, ensure_ascii=False),
                        reason,
                        detail,
                    ),
                )
                cursor.execute(f"DELETE FROM {table_name} WHERE id = ?", (data["id"],))
                quarantined += 1

        summary[table_name] = {
            "orphans_found": len(orphan_rows),
            "repaired": repaired,
            "quarantined": quarantined,
        }
    return summary


def run_migrations(engine=None):
    """升级入口。返回本次迁移摘要；已是最新则返回 None。"""
    from .database import engine as default_engine

    engine = engine or default_engine
    connection = engine.raw_connection()
    connection.row_factory = sqlite3.Row
    cursor = connection.cursor()
    try:
        # 迁移需要 DDL 内改外键，临时关闭该连接的外键强制，结束后恢复。
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")

        _ensure_ledger_tables(cursor)

        done = cursor.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (MIGRATION_VERSION,)
        ).fetchone()
        if done:
            result = None
        else:
            rebuilt = []
            for table_name in REBUILD_TABLES:
                if _needs_rebuild(cursor, table_name):
                    _rebuild_table(cursor, Base.metadata.tables[table_name])
                    rebuilt.append(table_name)

            summary = _scan_orphans(cursor)

            cursor.execute(
                "INSERT INTO schema_migrations (version, applied_at, detail) VALUES (?, ?, ?)",
                (MIGRATION_VERSION, datetime.utcnow().isoformat(sep=" "),
                 json.dumps({"rebuilt_tables": rebuilt, "orphans": summary}, ensure_ascii=False)),
            )
            result = {"version": MIGRATION_VERSION, "rebuilt_tables": rebuilt, "orphans": summary}
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        connection.close()

    # 触发器在迁移事务之外幂等创建（函数本身可独立重入）。
    ensure_triggers(engine)
    return result
