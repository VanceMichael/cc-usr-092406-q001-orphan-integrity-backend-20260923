"""统一父子规则、错误码区分、失败事务不污染后续请求。"""

import sqlite3
import unittest

from support import new_db_file, make_client, raw_connect


def create_pond_batch(client, number="B1", status="active"):
    pid = client.post("/api/ponds/", json={
        "name": f"P-{number}", "area": 10, "water_depth": 2,
        "species": "鲈鱼"}).json()["id"]
    bid = client.post("/api/batches/", json={
        "batch_number": number, "pond_id": pid, "species": "鲈鱼",
        "stocking_date": "2026-01-01", "status": status}).json()["id"]
    return pid, bid


# 六类明细的最小合法载荷（去掉 batch_id，由测试填）
CHILD_PAYLOADS = {
    "stocking-records": {"species": "鲈鱼", "quantity": 100},
    "feeding-records": {"feeding_date": "2026-02-01", "feed_type": "颗粒",
                        "feed_quantity": 5.0},
    "water-quality-records": {"record_date": "2026-02-01"},
    "medication-records": {"medication_date": "2026-02-01", "drug_name": "药A"},
    "cost-records": {"cost_date": "2026-02-01", "cost_type": "feed",
                     "amount": 12.5},
    "harvest-sales": {"sale_date": "2026-03-01", "weight": 10.0,
                      "unit_price": 8.0},
}


class IntegrityRulesTest(unittest.TestCase):
    def setUp(self):
        self.path = new_db_file("rules.db")
        self.client, _, self.Session = make_client(self.path)

    def _post(self, resource, batch_id):
        payload = dict(CHILD_PAYLOADS[resource])
        payload["batch_id"] = batch_id
        return self.client.post(f"/api/{resource}/", json=payload)

    def test_all_children_reject_missing_parent_with_stable_code(self):
        _, bid = create_pond_batch(self.client)
        for resource in CHILD_PAYLOADS:
            r = self._post(resource, 999999)
            self.assertEqual(r.status_code, 404, resource)
            self.assertEqual(r.json()["code"], "batch_not_found", resource)
            self.assertEqual(r.json()["batch_id"], 999999, resource)
            # 父存在时正常创建
            ok = self._post(resource, bid)
            self.assertEqual(ok.status_code, 200, (resource, ok.text))

    def test_closed_batch_rejects_new_and_relink(self):
        _, bid = create_pond_batch(self.client, "BC", status="closed")
        r = self._post("feeding-records", bid)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "batch_closed")

        # 改挂到已关闭批次同样拒绝
        _, active_bid = create_pond_batch(self.client, "BA")
        rec = self._post("feeding-records", active_bid).json()
        r = self.client.put(f"/api/feeding-records/{rec['id']}/",
                            json={"batch_id": bid})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "batch_closed")

    def test_deleting_batch_rejects_new_record(self):
        _, bid = create_pond_batch(self.client, "BD")
        conn = raw_connect(self.path)
        conn.execute("UPDATE batches SET status='deleting' WHERE id=?", (bid,))
        conn.commit()
        conn.close()
        r = self._post("feeding-records", bid)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "batch_deleting")

    def test_relink_to_nonexistent_batch_is_rejected(self):
        _, bid = create_pond_batch(self.client, "BR")
        rec = self._post("feeding-records", bid).json()
        r = self.client.put(f"/api/feeding-records/{rec['id']}/",
                            json={"batch_id": 424242})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "batch_not_found")
        # 原归属未被破坏
        self.assertEqual(
            self.client.get(f"/api/feeding-records/{rec['id']}/").json()["batch_id"],
            bid)

    def test_delete_with_children_is_stable_conflict_no_cascade(self):
        _, bid = create_pond_batch(self.client, "BX")
        self._post("feeding-records", bid)
        self._post("cost-records", bid)
        r = self.client.delete(f"/api/batches/{bid}/")
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertEqual(body["code"], "batch_has_children")
        self.assertEqual(body["child_counts"], {"投喂": 1, "成本": 1})
        # 重复删除得到同样的稳定冲突，批次与明细都还在
        r2 = self.client.delete(f"/api/batches/{bid}/")
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(
            self.client.get(f"/api/batches/{bid}/").status_code, 200)
        self.assertEqual(
            len(self.client.get("/api/feeding-records/",
                                params={"batch_id": bid}).json()), 1)

    def test_delete_empty_batch_succeeds(self):
        _, bid = create_pond_batch(self.client, "BE")
        r = self.client.delete(f"/api/batches/{bid}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/batches/{bid}/").status_code, 404)

    def test_pond_delete_conflicts_with_batches(self):
        pid, _ = create_pond_batch(self.client, "BP")
        r = self.client.delete(f"/api/ponds/{pid}/")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "pond_has_batches")

    def test_failed_transaction_does_not_pollute_later_requests(self):
        # 重复批次号触发 IntegrityError -> 回滚；连接归还后必须仍可正常服务。
        create_pond_batch(self.client, "DUP")
        r = self.client.post("/api/batches/", json={
            "batch_number": "DUP", "pond_id": 1, "species": "鲈鱼",
            "stocking_date": "2026-01-01"})
        self.assertEqual(r.status_code, 400)
        # 紧接着的正常请求不受污染
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        ok = self.client.post("/api/batches/", json={
            "batch_number": "DUP2", "pond_id": 1, "species": "鲈鱼",
            "stocking_date": "2026-01-01"})
        self.assertEqual(ok.status_code, 200)

    def test_direct_sql_insert_orphan_is_blocked(self):
        # 即使直连关闭 FK pragma，触发器也不允许写入孤儿。
        conn = raw_connect(self.path, foreign_keys=False)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO cost_records (batch_id,cost_date,cost_type,amount) "
                "VALUES (999,'2026-02-01','feed',1)")
        conn.close()

    def test_status_reports_constraints_and_zero_orphans(self):
        body = self.client.get("/api/integrity/status").json()
        self.assertEqual(body["schema_version"], 1)
        self.assertTrue(body["foreign_keys_enforced"])
        self.assertEqual(body["journal_mode"].lower(), "wal")
        self.assertEqual(body["deleting_batches"], 0)
        self.assertEqual(sum(body["live_orphan_records"].values()), 0)


if __name__ == "__main__":
    unittest.main()
