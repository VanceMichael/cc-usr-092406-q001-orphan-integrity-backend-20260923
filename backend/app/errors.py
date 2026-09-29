"""统一业务异常与 API 错误码。

API 用稳定的 code 区分四类情况：
  batch_not_found   父记录不存在
  batch_closed      批次已关闭（或正在删除），禁止新增/改挂
  batch_deleting    批次正在删除（两阶段删除进行中）
  batch_has_children 删除冲突：批次下仍有明细
  orphan_quarantined 历史孤儿已隔离，不能按正常记录访问
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, extra: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def batch_not_found(batch_id):
    return AppError(404, "batch_not_found", f"批次 {batch_id} 不存在",
                    {"batch_id": batch_id})


def batch_closed(batch):
    return AppError(409, "batch_closed",
                    f"批次 {batch.batch_number} 已关闭，不能新增或改挂记录",
                    {"batch_id": batch.id, "batch_number": batch.batch_number,
                     "batch_status": batch.status})


def batch_deleting(batch):
    return AppError(409, "batch_deleting",
                    f"批次 {batch.batch_number} 正在删除，请稍后重试",
                    {"batch_id": batch.id, "batch_number": batch.batch_number})


def batch_has_children(batch_id, counts: dict):
    return AppError(409, "batch_has_children",
                    "批次下仍有明细记录，拒绝删除（不会级联删除）",
                    {"batch_id": batch_id, "child_counts": counts})


def register_error_handlers(app):
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message, **exc.extra},
        )
