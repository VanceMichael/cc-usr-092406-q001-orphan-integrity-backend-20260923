"""统一父子规则：创建、改挂、删除与四类错误码。"""
import sqlite3
import unittest

from _support import FreshDatabaseCase


CHILD_ENDPOINTS = [
    (
        "/api/stocking-records/",
        {"species": "鲫鱼", "quantity": 100},
    ),
    (
        "/api/feeding-records/",
        {"feeding_date": "2026-03-02", "feed_type": "颗粒料", "feed_quantity": 5.0},
    ),
    (
        "/api/water-quality-records/",
        {"record_date": "2026-03-02", "ph_value": 7.2},
    ),
    (
        "/api/medication-records/",
        {"medication_date": "2026-03-02", "drug_name": "消毒剂", "dosage": 1.0},
    ),
    (
        "/api/cost-records/",
        {"cost_date": "2026-03-02", "cost_type": "feed", "amount": 88.5},
    ),
    (
        "/api/harvest-sales/",
        {"sale_date": "2026-06-01", "weight": 100.0, "unit_price": 12.0},
    ),
]


class ParentChildRuleTest(FreshDatabaseCase, unittest.TestCase):
    def test_create_on_missing_parent_is_404_with_code_for_all_children(self):
        for endpoint, payload in CHILD_ENDPOINTS:
            body = dict(payload, batch_id=99999)
            r = self.client.post(endpoint, json=body)
            self.assertEqual(r.status_code, 404, endpoint)
            self.assertEqual(r.json()["code"], "parent_not_found", endpoint)

    def test_create_and_read_happy_path_for_all_children(self):
        _, batch_id = self.make_pond_batch()
        for endpoint, payload in CHILD_ENDPOINTS:
            r = self.client.post(endpoint, json=dict(payload, batch_id=batch_id))
            self.assertEqual(r.status_code, 200, (endpoint, r.text))
            self.assertEqual(r.json()["batch_id"], batch_id)

    def test_closed_batch_rejects_new_children_with_stable_code(self):
        _, batch_id = self.make_pond_batch(status="closed")
        for endpoint, payload in CHILD_ENDPOINTS:
            r = self.client.post(endpoint, json=dict(payload, batch_id=batch_id))
            self.assertEqual(r.status_code, 409, endpoint)
            self.assertEqual(r.json()["code"], "batch_closed", endpoint)

    def test_reparent_to_missing_and_closed_batch(self):
        _, b1 = self.make_pond_batch("B-001")
        _, b2 = self.make_pond_batch("B-002")
        # close b2
        r = self.client.put(f"/api/batches/{b2}/", json={"status": "closed"})
        self.assertEqual(r.status_code, 200)

        r = self.client.post(
            "/api/feeding-records/",
            json={
                "batch_id": b1,
                "feeding_date": "2026-03-02",
                "feed_type": "A",
                "feed_quantity": 1.0,
            },
        )
        record_id = r.json()["id"]

        # reparent onto nonexistent batch -> 404 parent_not_found
        r = self.client.put(
            f"/api/feeding-records/{record_id}/", json={"batch_id": 424242}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "parent_not_found")

        # reparent onto closed batch -> 409 batch_closed
        r = self.client.put(
            f"/api/feeding-records/{record_id}/", json={"batch_id": b2}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "batch_closed")

        # reparent onto another active batch works
        _, b3 = self.make_pond_batch("B-003")
        r = self.client.put(
            f"/api/feeding-records/{record_id}/", json={"batch_id": b3}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["batch_id"], b3)

    def test_reparent_out_of_closed_batch_rejected(self):
        _, b1 = self.make_pond_batch("B-010")
        _, b2 = self.make_pond_batch("B-011")
        # create feeding in b1, then close b1
        fid = self.client.post(
            "/api/feeding-records/",
            json={
                "batch_id": b1,
                "feeding_date": "2026-03-02",
                "feed_type": "A",
                "feed_quantity": 1.0,
            },
        ).json()["id"]
        self.assertEqual(
            self.client.put(f"/api/batches/{b1}/", json={"status": "closed"}).status_code,
            200,
        )
        r = self.client.put(f"/api/feeding-records/{fid}/", json={"batch_id": b2})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "batch_closed")

    def test_delete_nonempty_batch_is_conflict_never_cascade(self):
        _, batch_id = self.make_pond_batch()
        self.client.post(
            "/api/cost-records/",
            json={
                "batch_id": batch_id,
                "cost_date": "2026-03-02",
                "cost_type": "feed",
                "amount": 10,
            },
        )
        r = self.client.delete(f"/api/batches/{batch_id}/")
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertEqual(body["code"], "batch_has_children")
        self.assertEqual(body["details"]["child_counts"]["cost_records"], 1)

        # 批次与明细都还在：没有级联、没有孤儿
        self.assertEqual(self.client.get(f"/api/batches/{batch_id}/").status_code, 200)
        con = self.raw_external_connection(foreign_keys=True)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM cost_records WHERE batch_id=?", (batch_id,)
                ).fetchone()[0],
                1,
            )
        finally:
            con.close()

    def test_delete_empty_batch_succeeds_then_children_seen_as_orphan(self):
        _, batch_id = self.make_pond_batch()
        self.assertEqual(self.client.delete(f"/api/batches/{batch_id}/").status_code, 200)
        # 再创建明细 -> 父不存在
        r = self.client.post(
            "/api/feeding-records/",
            json={
                "batch_id": batch_id,
                "feeding_date": "2026-03-02",
                "feed_type": "A",
                "feed_quantity": 1.0,
            },
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "parent_not_found")


class RawWriteAndTriggerTest(FreshDatabaseCase, unittest.TestCase):
    def test_external_fk_off_connection_cannot_insert_orphan_or_delete_parent(self):
        _, batch_id = self.make_pond_batch()
        con = self.raw_external_connection(foreign_keys=False)  # SQLite 默认
        try:
            self.assertEqual(con.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            with self.assertRaises(sqlite3.Error):
                con.execute(
                    "INSERT INTO feeding_records"
                    "(batch_id,feeding_date,feed_type,feed_quantity)"
                    " VALUES(424242,'2026-01-01','x',1)"
                )
            con.rollback()
            con.execute(
                "INSERT INTO feeding_records"
                "(batch_id,feeding_date,feed_type,feed_quantity)"
                " VALUES(?,?,?,?)",
                (batch_id, "2026-03-02", "A", 2.0),
            )
            con.commit()
            with self.assertRaises(sqlite3.Error):
                con.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            con.rollback()
        finally:
            con.close()
        # 批次仍在，且只多出一条合法明细
        self.assertEqual(self.client.get(f"/api/batches/{batch_id}/").status_code, 200)

    def test_database_level_restict_works_with_fk_on_too(self):
        _, batch_id = self.make_pond_batch()
        con = self.raw_external_connection(foreign_keys=True)
        try:
            con.execute(
                "INSERT INTO cost_records(batch_id,cost_date,cost_type,amount)"
                " VALUES(?,?,?,?)",
                (batch_id, "2026-03-02", "feed", 5.0),
            )
            con.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("DELETE FROM batches WHERE id=?", (batch_id,))
            con.rollback()
        finally:
            con.close()

    def test_historical_orphan_read_returns_distinct_code(self):
        _, batch_id = self.make_pond_batch()
        # 人为制造一个绕过一切校验的历史孤儿（迁移之前遗留的同类数据）。
        con = self.raw_external_connection(foreign_keys=False)
        try:
            con.execute("DROP TRIGGER IF EXISTS trg_feeding_records_insert_parent")
            con.execute(
                "INSERT INTO feeding_records"
                "(batch_id,feeding_date,feed_type,feed_quantity)"
                " VALUES(31337,'2026-01-01','legacy',1)"
            )
            con.commit()
            orphan_id = con.execute(
                "SELECT id FROM feeding_records WHERE batch_id=31337"
            ).fetchone()[0]
        finally:
            con.close()

        r = self.client.get(f"/api/feeding-records/{orphan_id}/")
        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.json()["code"], "historical_orphan")
        self.assertEqual(r.json()["details"]["orphan_batch_id"], 31337)


class SessionHygieneTest(FreshDatabaseCase, unittest.TestCase):
    def test_failed_write_does_not_poison_later_request(self):
        _, batch_id = self.make_pond_batch()
        # 一次必然失败的请求（父不存在）
        r = self.client.post(
            "/api/cost-records/",
            json={
                "batch_id": 77777,
                "cost_date": "2026-03-02",
                "cost_type": "feed",
                "amount": 1,
            },
        )
        self.assertEqual(r.status_code, 404)
        # 紧接着的合法请求必须正常提交
        r = self.client.post(
            "/api/cost-records/",
            json={
                "batch_id": batch_id,
                "cost_date": "2026-03-03",
                "cost_type": "feed",
                "amount": 2,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
