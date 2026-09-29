from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .database import engine, Base
from .errors import register_error_handlers
from .migration import ensure_integrity_triggers_for_engine
from .routers import (
    ponds, batches, stocking, feeding, water_quality, medication,
    costs, harvest, analysis, integrity,
)

Base.metadata.create_all(bind=engine)
# 新库在此安装直写防线触发器；旧库迁移时已安装，此处幂等补齐。
ensure_integrity_triggers_for_engine(engine)

app = FastAPI(
    title="水产养殖管理系统",
    description=(
        "支持塘口管理、投苗记录、日常管理、成本核算、出塘销售和养殖周期分析。"
        "全部父子记录由数据库 RESTRICT 外键与统一服务层双重保证引用完整性。"
    ),
    version="2.0.0"
)

register_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ponds.router)
app.include_router(batches.router)
app.include_router(stocking.router)
app.include_router(feeding.router)
app.include_router(water_quality.router)
app.include_router(medication.router)
app.include_router(costs.router)
app.include_router(harvest.router)
app.include_router(analysis.router)
app.include_router(integrity.router)


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
