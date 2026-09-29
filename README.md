# q001 水产养殖服务

本项目是水产养殖管理后端，维护塘口、养殖批次、投苗、投喂、水质、用药、成本、销售与周期分析数据。业务数据保存在 SQLite 文件中，HTTP 接口由 FastAPI 提供。

## 测试命令

```bash
python3 -m unittest discover -s tests -v
```

## 编译与构建命令

```bash
python3 -m compileall -q backend/app
```

## 启动命令

```bash
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后可访问 `/health` 检查服务状态。开发环境不得提交真实账号、连接凭据或生产数据。

## 引用完整性规则

投苗、投喂、水质、用药、成本、销售六类明细与批次之间，以及批次与塘口之间，
遵循同一条父子规则（数据库 `ON DELETE RESTRICT` 外键 + 触发器 + 统一服务层三重保证）：

- **创建 / 改挂明细**：父批次必须存在且未关闭（`closed`）、未在删除（`deleting`）。
- **删除明细**：允许（子记录可单独删除）。
- **删除批次**：先置 `deleting` 公告意图并提交，再持锁复查子女；仍有明细时
  恢复原状态并返回 `409 batch_has_children`，绝不级联、不留孤儿；无明细才真正删除。
- **删除塘口**：仍有批次时返回 `409 pond_has_batches`。
- 直接用 `sqlite3` 写库（即便不开启 `PRAGMA foreign_keys`）也会被触发器拦截。
- 写事务统一 `BEGIN IMMEDIATE` 串行化，清理批次与现场补录竞争时只有一方提交。

### 错误码

HTTP 响应体含稳定 `code`：`batch_not_found`（父不存在）、`batch_closed`（已关闭）、
`batch_deleting`（正在删除）、`batch_has_children`（删除冲突）、
`pond_has_batches`、`record_not_found`。

### 旧库升级

启动时按 `PRAGMA user_version` 自动迁移：扫描现存孤儿，能凭记录上的批次号
**唯一归属**的安全补正；无法判断（无批次号 / 查无 / 命中多个）的整行快照迁入
`orphan_quarantine` 隔离并记录原因，随后按带 `RESTRICT` 的结构重建表，
并恢复崩溃残留的 `deleting` 批次。迁移与每次启动恢复都写入 `migration_log`。

### 完整性管理接口

- `GET /api/integrity/status`：约束开关、隔离与复核进度、现存孤儿计数。
- `GET /api/integrity/migration-history`：迁移与启动恢复记录（重启后可核对）。
- `GET /api/integrity/quarantine/`：隔离记录列表（可按 `status_filter`、`record_type` 过滤）。
- `POST /api/integrity/quarantine/{id}/resolve`：人工复核，
  `{"action":"reattach","target_batch_id":N}` 补挂，或 `{"action":"discard"}` 作废。

