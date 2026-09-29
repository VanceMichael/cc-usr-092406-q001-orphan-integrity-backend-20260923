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

投苗、投喂、水质、用药、成本、销售六类明细与养殖批次之间执行统一父子规则，
由数据库 `ON DELETE RESTRICT` 外键、SQLite 触发器与接口校验三重保证
（外部程序关闭 `PRAGMA foreign_keys` 直连写库同样会被触发器拦截）：

- 创建明细：父批次必须存在（`404 parent_not_found`）、未封档（`409 batch_closed`）、
  不在删除中（`409 batch_deleting`）。
- 改挂明细：改入/改出已关闭批次均拒绝；改挂到不存在批次返回 `parent_not_found`。
- 删除批次：仍有明细时返回稳定冲突 `409 batch_has_children`（带各表明细数），
  绝不级联、不留孤儿；空批次才可删。
- 现存明细的父批次已消失时，读取返回 `410 historical_orphan`，引导到隔离区复核。

写事务一律以 `BEGIN IMMEDIATE` 开启，配合 WAL 与 `busy_timeout`，
“清理批次 vs 现场补录”并发时在 SQLite 层排队，只有一方提交，
失败事务立即回滚，不污染后续请求。

## 旧库迁移与隔离复核

服务启动时自动幂等迁移（版本记录于 `schema_migrations` 表）：
重建旧表补齐 `ON DELETE RESTRICT`，随后扫描现存孤儿——

- 明细携带的批次号能唯一命中间断批次：同事务安全补正 `batch_id`，留 `repaired` 台账；
- 批次号缺失或查无此批：原始行 JSON 快照写入 `quarantine_records`（`pending`）并保留原因，
  从明细表移除，等待人工复核。

可核对与复核端点：

- `GET /api/integrity/status/`：外键开关、迁移版本、隔离与复核进度汇总。
- `GET /api/integrity/quarantine/?review_status=pending`：隔离明细列表（含原始快照与原因）。
- `POST /api/integrity/quarantine/{id}/review/`：`{"action":"repair","batch_id":N}`
  安全补正归属，或 `{"action":"reject"}` 确认丢弃；重复复核返回冲突。

删除采用两阶段（先持久化 `deleting` 再复查删除）；若进程在中间崩溃，
下次启动会把残留 `deleting` 批次对账恢复为 `active`，绝不替用户自动删除。
