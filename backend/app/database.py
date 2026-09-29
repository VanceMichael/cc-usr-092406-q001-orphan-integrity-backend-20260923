import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./aquaculture.db")

if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    raise RuntimeError("本服务仅支持 SQLite 数据库")

# 解析数据库文件路径（sqlite:///相对路径 / sqlite:////绝对路径 / :memory:）
if SQLALCHEMY_DATABASE_URL == "sqlite:///:memory:":
    _DB_PATH = ":memory:"
else:
    _DB_PATH = SQLALCHEMY_DATABASE_URL.replace("sqlite:///", "", 1)
    if _DB_PATH:
        Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# Base 先行定义：迁移器导入 ORM 模型时只依赖 Base，避免循环导入。
Base = declarative_base()

# 每请求（每线程）的写意图标记：仅写事务使用 BEGIN IMMEDIATE，
# 只读事务保持 DEFERRED，不互相阻塞。
_tx_state = threading.local()


def mark_write_intent():
    """声明当前请求要执行写操作，下一个事务以 BEGIN IMMEDIATE 开始。

    所有创建/改挂/删除路径在第一条 SQL 前调用，确保清理批次与现场补录
    竞争时只有一方拿到 RESERVED 锁。
    """
    _tx_state.immediate = True


def _clear_tx_state():
    _tx_state.immediate = False


def build_sqlite_engine(url: str):
    """构造带完整性 PRAGMA 与按写意图发 BEGIN IMMEDIATE 的引擎。

    全局引擎与测试/重启场景的临时引擎共用同一套连接与事务约定。
    """
    is_memory = url == "sqlite:///:memory:"
    eng = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        # 外键约束默认每连接关闭，必须显式打开，连接池复用时同样生效。
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL：读写不互相阻塞；写写之间仍由 BEGIN IMMEDIATE 串行。
        cursor.execute("PRAGMA journal_mode=WAL")
        # 锁竞争时等待而非立即报 SQLITE_BUSY，让负方拿到锁后重读已提交事实。
        cursor.execute("PRAGMA busy_timeout=10000")
        if is_memory:
            cursor.execute("PRAGMA user_version=1")
        cursor.close()
        # 关闭 pysqlite 自动 BEGIN，由 begin 监听器按写意图统一发号。
        dbapi_connection.isolation_level = None

    @event.listens_for(eng, "begin")
    def _begin(conn):
        if getattr(_tx_state, "immediate", False):
            # 写事务立即加 RESERVED 锁：并发的清理与补录只有一方能进入，
            # 另一方在锁外等待，提交后重读事实并拿到明确业务冲突。
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.exec_driver_sql("BEGIN")

    return eng


# 文件型数据库：建业务引擎前先完成旧库迁移（孤儿扫描/隔离、补 RESTRICT 约束）。
# 迁移使用独立连接，避免与业务引擎的事务钩子互相干扰。
if _DB_PATH != ":memory:":
    from .migration import migrate_database
    migrate_database(_DB_PATH)

engine = build_sqlite_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    _clear_tx_state()
    db = SessionLocal()
    try:
        yield db
    except Exception:
        # 失败事务必须回滚，连接归还连接池时不得污染后续请求。
        db.rollback()
        raise
    finally:
        db.close()
        _clear_tx_state()


def db_file_path() -> str:
    return _DB_PATH
