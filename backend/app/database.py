import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from fastapi import Request

Base = declarative_base()

DEFAULT_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./aquaculture.db")

# 双引擎：
# - engine（写）：每个事务以 BEGIN IMMEDIATE 开启，第一个写者立即取得保留锁，
#   其它写者在 busy_timeout 内排队，保证“清理 vs 现场补录”只有一方提交；
# - read_engine（读）：普通 DEFERRED 事务，借助 WAL 与写者并发读取快照。
engine = None
SessionLocal = None
read_engine = None
ReadSessionLocal = None


def _sqlite_file_path(url: str):
    if not url.startswith("sqlite:///"):
        return None
    tail = url[len("sqlite:///"):]
    if tail in ("", ":memory:"):
        return None
    return tail


def _attach_pragmas(target_engine, db_file: str, immediate: bool):
    @event.listens_for(target_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, connection_record):
        # 事务边界由应用/事件控制，关闭驱动的自动 BEGIN 拦截。
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            # SQLite 外键强制是连接级开关，逐连接打开，
            # 绕过接口直接写库同样会被 ON DELETE RESTRICT 拦截。
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            if db_file:
                cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    if immediate:

        @event.listens_for(target_engine, "begin")
        def _begin_immediate(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    else:

        @event.listens_for(target_engine, "begin")
        def _begin_deferred(conn):
            conn.exec_driver_sql("BEGIN")


def configure(url: str = None):
    """按给定 URL 重建读写引擎；测试与重启都通过它绑定同一套连接约定。"""
    global engine, SessionLocal, read_engine, ReadSessionLocal

    url = url or DEFAULT_DATABASE_URL
    db_file = _sqlite_file_path(url)
    if db_file:
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)

    common = {"connect_args": {"check_same_thread": False}}

    engine = create_engine(url, **common)
    # :memory: 数据库各自独立，双引擎会看不到同一份数据，此时共用一个引擎。
    read_engine = engine if not db_file else create_engine(url, **common)

    if url.startswith("sqlite"):
        _attach_pragmas(engine, db_file, immediate=True)
        if read_engine is not engine:
            _attach_pragmas(read_engine, db_file, immediate=False)

    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    ReadSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=read_engine)
    return engine


configure()


def get_db(request: Request):
    """请求级会话：GET/HEAD 走 WAL 读引擎，其它方法走 IMMEDIATE 写引擎。"""
    method = (request.method or "").upper()
    factory = ReadSessionLocal if method in ("GET", "HEAD") else SessionLocal
    db = factory()
    try:
        yield db
    except Exception:
        # 失败事务必须回滚，不能污染复用同一引擎的后续请求。
        db.rollback()
        raise
    finally:
        db.close()


def get_read_db():
    db = ReadSessionLocal()
    try:
        yield db
    finally:
        db.close()
