"""旧库迁移：补 RESTRICT 约束、孤儿补正/隔离、幂等、重启可核对。"""
import sqlite3
import unittest

from _support import OldDatabaseCase


class MigrationTest(OldDatabaseCase, unittest.TestCase):
    def test_rebuilds_tables_with_on_delete_restrict(self):
        con = sqlite3.connect(self.db_path)
        try:
            for table in (
                "stocking_records",
                "feeding_records",
                "water_quality_records",
                "medication_records",
                "cost_records",
                "harvest_sales",
            ):
                ddl = con.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()[0]
                self.assertIn("ON DELETE RESTRICT", ddl, table)
        finally:
            con.close()

    def test_orphans_with_unique_hint_are_safely_repaired(self):
        con = sqlite3.connect(self.db_path)
        try:
            # 原 batch_id=77 的孤儿按唯一批次号 B-REAL 补正到批次 1
            self.assertEqual(
                con.execute("SELECT batch_id FROM harvest_sales WHERE id=10").fetchone()[0],
                1,
            )
            self.assertEqual(
                con.execute(
                    "SELECT batch_id FROM stocking_records WHERE id=20"
                ).fetchone()[0],
                1,
            )
            # 健康明细不受影响
            self.assertEqual(
                con.execute("SELECT batch_id FROM feeding_records WHERE id=1").fetchone()[0],
                1,
            )
        finally:
            con.close()

        repaired = self.client.get(
            "/api/integrity/quarantine/?review_status=repaired"
        ).json()
        reasons = {(row["source_table"], row["source_id"]) for row in repaired}
        self.assertIn(("harvest_sales", 10), reasons)
        self.assertIn(("stocking_records", 20), reasons)

    def test_unresolvable_orphans_are_quarantined_with_reasons(self):
        con = sqlite3.connect(self.db_path)
        try:
            # 无法归属的两行已从明细表移除，不留孤儿
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM feeding_records WHERE id=2"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM harvest_sales WHERE id=11"
                ).fetchone()[0],
                0,
            )
        finally:
            con.close()

        pending = self.client.get(
            "/api/integrity/quarantine/?review_status=pending"
        ).json()
        by_key = {(row["source_table"], row["source_id"]): row for row in pending}
        feeding = by_key[("feeding_records", 2)]
        sale = by_key[("harvest_sales", 11)]
        self.assertEqual(feeding["reason"], "no_batch_number_hint")
        self.assertIn("无法判断归属", feeding["reason_detail"])
        self.assertEqual(sale["reason"], "batch_number_not_found")
        self.assertEqual(sale["orphan_batch_id"], 88)
        # 原始数据完整保留在快照里
        import json

        payload = json.loads(sale["payload"])
        self.assertEqual(payload["batch_number"], "B-GONE")
        self.assertEqual(payload["weight"], 4)

    def test_no_orphan_remains_and_status_reports_progress(self):
        con = sqlite3.connect(self.db_path)
        try:
            for table in (
                "stocking_records",
                "feeding_records",
                "water_quality_records",
                "medication_records",
                "cost_records",
                "harvest_sales",
            ):
                orphans = con.execute(
                    f"SELECT count(*) FROM {table} WHERE batch_id NOT IN "
                    "(SELECT id FROM batches)"
                ).fetchone()[0]
                self.assertEqual(orphans, 0, table)
        finally:
            con.close()

        status = self.client.get("/api/integrity/status/").json()
        self.assertTrue(status["foreign_keys_enforced"])
        self.assertIn("2_referential_integrity", status["migration_versions"])
        self.assertEqual(status["quarantine"]["pending"], 2)
        self.assertEqual(status["quarantine"]["repaired"], 2)
        self.assertEqual(
            status["latest_migration"]["detail"]["orphans"]["feeding_records"],
            {"orphans_found": 1, "repaired": 0, "quarantined": 1},
        )

    def test_migration_is_idempotent(self):
        from app.migration import run_migrations
        from app import database as dbmod

        # 第二次启动不应再处理任何行
        self.assertIsNone(run_migrations(dbmod.engine))
        status = self.client.get("/api/integrity/status/").json()
        self.assertEqual(status["quarantine"]["pending"], 2)

    def test_restart_preserves_constraints_quarantine_and_review_progress(self):
        # 复核一条（reject），再重启
        pending = self.client.get(
            "/api/integrity/quarantine/?review_status=pending"
        ).json()
        feeding = next(row for row in pending if row["source_table"] == "feeding_records")
        r = self.client.post(
            f"/api/integrity/quarantine/{feeding['id']}/review/",
            json={"action": "reject", "reviewed_by": "admin"},
        )
        self.assertEqual(r.status_code, 200)

        self.restart_service()

        status = self.client.get("/api/integrity/status/").json()
        self.assertTrue(status["foreign_keys_enforced"])
        self.assertIn("2_referential_integrity", status["migration_versions"])
        self.assertEqual(status["quarantine"]["rejected"], 1)
        self.assertEqual(status["quarantine"]["pending"], 1)

        # 复核结果持久：不能再次复核同一条
        r = self.client.post(
            f"/api/integrity/quarantine/{feeding['id']}/review/",
            json={"action": "reject"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "quarantine_already_reviewed")

    def test_manual_repair_reinserts_row_under_target_batch(self):
        pending = self.client.get(
            "/api/integrity/quarantine/?review_status=pending"
        ).json()
        sale = next(
            row
            for row in pending
            if row["source_table"] == "harvest_sales"
            and row["reason"] == "batch_number_not_found"
        )
        r = self.client.post(
            f"/api/integrity/quarantine/{sale['id']}/review/",
            json={"action": "repair", "batch_id": 1, "reviewed_by": "admin"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["resolved_batch_id"], 1)

        # 通过销售接口能查到已归位的记录
        sales = self.client.get("/api/harvest-sales/?batch_id=1").json()
        self.assertTrue(any(s["batch_number"] == "B-GONE" for s in sales))

    def test_migrated_db_enforces_restrict_against_raw_delete(self):
        con = sqlite3.connect(self.db_path)
        try:
            # feeding_records id=1 仍引用批次 1，直连删除父批次必须被拦截
            with self.assertRaises(sqlite3.Error):
                con.execute("DELETE FROM batches WHERE id=1")
            con.rollback()
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
