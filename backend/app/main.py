from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from .database import engine, Base, SessionLocal
from .migration import run_migrations
from .integrity import IntegrityConflict, reconcile_pending_deletions
from .routers import (
    ponds,
    batches,
    stocking,
    feeding,
    water_quality,
    medication,
    costs,
    harvest,
    analysis,
    integrity as integrity_router,
)

# 全新库由元数据建表（已含 ON DELETE RESTRICT）；旧库随后由迁移补齐
# 约束并扫描孤儿。重启重跑迁移为幂等空操作。
Base.metadata.create_all(bind=engine)
run_migrations(engine)
# 恢复崩溃残留在 deleting 中间态的批次，绝不替用户自动删除。
_startup = SessionLocal()
try:
    reconcile_pending_deletions(_startup)
finally:
    _startup.close()

app = FastAPI(
    title="水产养殖管理系统",
    description="支持塘口、批次、投苗、投喂、水质、用药、成本、销售与周期分析；"
    "明细与批次之间执行统一父子引用规则。",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(IntegrityConflict)
async def integrity_conflict_handler(request: Request, exc: IntegrityConflict):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(OperationalError)
async def operational_error_handler(request: Request, exc: OperationalError):
    # BEGIN IMMEDIATE 竞争超时：明确告知调用方冲突，调用方可重试，
    # 而不是返回未分类的 500。
    message = str(getattr(exc, "orig", exc))
    if "locked" in message.lower() or "busy" in message.lower():
        return JSONResponse(
            status_code=409,
            content={
                "code": "concurrent_modification",
                "message": "数据正被另一事务修改，请重试",
                "details": {"reason": "database_locked"},
            },
        )
    raise exc


app.include_router(ponds.router)
app.include_router(batches.router)
app.include_router(stocking.router)
app.include_router(feeding.router)
app.include_router(water_quality.router)
app.include_router(medication.router)
app.include_router(costs.router)
app.include_router(harvest.router)
app.include_router(analysis.router)
app.include_router(integrity_router.router)


@app.get("/")
def root():
    return {
        "message": "欢迎使用水产养殖管理系统API",
        "docs": "/docs",
        "version": "2.0.0"
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}
