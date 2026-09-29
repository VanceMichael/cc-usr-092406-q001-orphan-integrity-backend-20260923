"""旧库升级：孤儿扫描、唯一归属补正、无法判断隔离留因、崩溃恢复、重启可核对。"""

import json
import sqlite3
import unittest

from support import build_legacy_db, new_db_file, raw_connect, make_client


SEED = """
INSERT INTO ponds VALUES (1,'P1',10,2,'鲈鱼','active',NULL,NULL);
INSERT INTO batches VALUES (1,'B-EXIST',1,'鲈鱼','2026-01-01',NULL,NULL,'active',NULL,NULL);
INSERT INTO batches VALUES (2,'B-HALFDEL',1,'草鱼','2026-01-01',NULL,NULL,'deleting',NULL,NULL);
-- 唯一批次号可归属 -> 安全补正到批次 1
INSERT INTO stocking_records VALUES (10,99,'鲈鱼',500,'苗场A','B-EXIST',1.0,500,NULL,NULL);
-- 无批次号文本 -> 隔离 no_batch_number_hint
INSERT INTO feeding_records VALUES (20,88,'2026-02-01','颗粒',5.0,NULL,NULL,NULL,NULL,NULL);
-- 批次号查无 -> 隔离 batch_number_not_found
INSERT INTO medication_records VALUES (30,77,'2026-02-02','药A',NULL,1.0,'kg',NULL,NULL,NULL,'B-GONE',NULL,NULL);
-- 销售孤儿同样按批次号补正
INSERT INTO harvest_sales VALUES (40,66,'2026-03-01',10,5,50,NULL,'B-EXIST',NULL,NULL,NULL);
-- 正常记录原样保留
INSERT INTO feeding_records VALUES (21,1,'2026-03-01','颗粒',6.0,NULL,NULL,NULL,NULL,NULL);
"""


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.path = new_db_file("legacy.db")
        build_legacy_db(self.path, SEED)

    def _open(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def test_v0_to_v1_scans_repairs_and_quarantines_with_reasons(self):
        from app.migration import migrate_database
        result = migrate_database(str(self.path))
        self.assertIsNotNone(result["migrated"])

        conn = self._open()
        # 版本升级
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        # 唯一归属：batch_id 99/66 被补正为 1
        self.assertEqual(
            conn.execute("SELECT batch_id FROM stocking_records WHERE id=10")
            .fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT batch_id FROM harvest_sales WHERE id=40")
            .fetchone()[0], 1)
        # 无法判断的两行被迁出原表并隔离，且保留原因
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM feeding_records WHERE id=20")
            .fetchone()[0], 0)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM medication_records WHERE id=30")
            .fetchone()[0], 0)
        rows = conn.execute(
            "SELECT record_type, source_id, missing_batch_ref, reason, payload_json "
            "FROM orphan_quarantine ORDER BY id").fetchall()
        reasons = {r["record_type"]: dict(r) for r in rows}
        self.assertEqual(reasons["feeding"]["reason"], "no_batch_number_hint")
        self.assertEqual(reasons["feeding"]["missing_batch_ref"], 88)
        self.assertEqual(reasons["medication"]["reason"], "batch_number_not_found")
        self.assertEqual(reasons["medication"]["missing_batch_ref"], 77)
        # 快照保留原始行内容
        payload = json.loads(reasons["feeding"]["payload_json"])
        self.assertEqual(payload["feed_quantity"], 5.0)
        self.assertEqual(payload["batch_id"], 88)
        # 正常记录保留
        self.assertEqual(
            conn.execute("SELECT batch_id FROM feeding_records WHERE id=21")
            .fetchone()[0], 1)
        # 崩溃残留的 deleting 批次恢复为 active
        status = conn.execute(
            "SELECT status, pre_delete_status FROM batches WHERE id=2").fetchone()
        self.assertEqual(status[0], "active")
        self.assertIsNone(status[1])
        conn.close()

    def test_restrict_foreign_keys_rebuilt_into_schema(self):
        from app.migration import migrate_database
        migrate_database(str(self.path))
        conn = self._open()
        for table in ("stocking_records", "feeding_records", "water_quality_records",
                      "medication_records", "cost_records", "harvest_sales"):
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()[0]
            self.assertIn("ON DELETE RESTRICT", ddl, table)
        conn.close()

    def test_triggers_block_direct_writes_even_with_fk_pragma_off(self):
        from app.migration import migrate_database
        migrate_database(str(self.path))
        # 数据管理员直连，foreign_keys 默认关闭，触发器仍应拦截。
        conn = raw_connect(self.path, foreign_keys=False)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO feeding_records (batch_id,feeding_date,feed_type,"
                "feed_quantity) VALUES (999,'2026-02-01','x',1)")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE feeding_records SET batch_id=998 WHERE id=21")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM batches WHERE id=1")  # 仍有明细
        conn.close()

    def test_migration_is_idempotent_and_logs_once(self):
        from app.migration import migrate_database
        migrate_database(str(self.path))
        second = migrate_database(str(self.path))
        self.assertIsNone(second["migrated"])  # v1 不再重跑迁移
        conn = self._open()
        n = conn.execute(
            "SELECT COUNT(*) FROM migration_log WHERE event='migrate'").fetchone()[0]
        self.assertEqual(n, 1)
        conn.close()

    def test_startup_recovers_deleting_batches_and_logs_progress(self):
        from app.migration import migrate_database
        migrate_database(str(self.path))  # 初始迁移
        # 模拟两阶段删除在阶段一后崩溃：写入 deleting。
        conn = raw_connect(self.path)
        conn.execute("UPDATE batches SET status='deleting' WHERE id=1")
        conn.commit()
        conn.close()

        result = migrate_database(str(self.path))  # 重启
        self.assertEqual(result["recovered_deleting"], 1)
        conn = self._open()
        self.assertEqual(
            conn.execute("SELECT status FROM batches WHERE id=1").fetchone()[0],
            "active")
        events = conn.execute(
            "SELECT event, detail_json FROM migration_log "
            "WHERE event='startup-recover'").fetchall()
        self.assertEqual(len(events), 1)
        self.assertIn("recovered_deleting_batches", events[0][1])
        conn.close()

    def test_quarantine_and_review_survive_restart(self):
        # 迁移后产生隔离记录，重开服务复核，结果仍在。
        client, _, _ = make_client(self.path)
        pending = client.get("/api/integrity/quarantine/").json()
        self.assertEqual(len(pending), 2)
        feeding_q = next(q for q in pending if q["record_type"] == "feeding")
        qid = feeding_q["id"]

        # 重新打开服务（模拟重启）
        client.app.dependency_overrides.clear()
        client, _, _ = make_client(self.path)
        still = client.get(f"/api/integrity/quarantine/").json()
        self.assertTrue(any(q["id"] == qid and q["status"] == "pending"
                            for q in still))

        r = client.post(f"/api/integrity/quarantine/{qid}/resolve",
                        json={"action": "discard", "note": "无法追溯"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "discarded")

        # 再重启一次，复核结果持久保留
        client.app.dependency_overrides.clear()
        client, _, _ = make_client(self.path)
        item = next(q for q in client.get("/api/integrity/quarantine/").json()
                    if q["id"] == qid)
        self.assertEqual(item["status"], "discarded")
        self.assertEqual(item["resolution_note"], "无法追溯")
        summary = client.get("/api/integrity/status").json()["quarantine"]
        self.assertEqual(summary["discarded"], 1)
        self.assertEqual(summary["pending"], 1)

    def test_reattach_quarantined_record_restores_to_live_table(self):
        client, _, _ = make_client(self.path)
        med = next(q for q in client.get("/api/integrity/quarantine/").json()
                   if q["record_type"] == "medication")
        r = client.post(f"/api/integrity/quarantine/{med['id']}/resolve",
                        json={"action": "reattach", "target_batch_id": 1,
                              "note": "人工确认"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "resolved")
        new_id = body["new_record_id"]
        rec = client.get(f"/api/medication-records/{new_id}/").json()
        self.assertEqual(rec["batch_id"], 1)
        self.assertEqual(rec["drug_name"], "药A")


if __name__ == "__main__":
    unittest.main()
