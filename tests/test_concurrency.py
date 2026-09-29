"""并发决胜：清理与现场补录竞争时只有一方提交，失败不污染后续。"""
import threading
import time
import unittest
from datetime import date

from sqlalchemy.orm import Session

from _support import FreshDatabaseCase
from app import database as dbmod
from app import integrity as service
from app.models import Batch, FeedingRecord


class ConcurrentDeleteVsInsertTest(FreshDatabaseCase, unittest.TestCase):
    def _insert_feeding(self, db, batch_id, feed_type="A"):
        return service.create_child(
            db,
            FeedingRecord,
            {
                "batch_id": batch_id,
                "feeding_date": date(2026, 3, 2),
                "feed_type": feed_type,
                "feed_quantity": 3.0,
            },
        )

    def test_insert_in_flight_wins_delete_conflicts_and_batch_recovers(self):
        _, batch_id = self.make_pond_batch()
        errors = []
        lock_held = threading.Event()
        release = threading.Event()

        def creator():
            db = Session(bind=dbmod.engine)
            try:
                # 第一条语句即触发 BEGIN IMMEDIATE，拿到写锁。
                db.get(Batch, batch_id)
                lock_held.set()
                release.wait(timeout=5)
                self._insert_feeding(db, batch_id, "concurrent")
            except Exception as exc:  # noqa: BLE001
                errors.append(("create", type(exc).__name__))
                db.rollback()
            finally:
                db.close()

        def deleter():
            # 等创建者持锁后再发起删除，使其排队等待。
            lock_held.wait(timeout=5)
            time.sleep(0.2)
            db = Session(bind=dbmod.engine)
            try:
                service.delete_batch(db, batch_id)
                errors.append(("delete", "committed"))
            except Exception as exc:  # noqa: BLE001
                errors.append(("delete", getattr(exc, "code", type(exc).__name__)))
            finally:
                db.close()

        t_create = threading.Thread(target=creator)
        t_delete = threading.Thread(target=deleter)
        t_create.start()
        self.assertTrue(lock_held.wait(timeout=5))
        t_delete.start()
        # 删除者已在排队，此时放行创建者提交。
        release.set()
        t_create.join(timeout=10)
        t_delete.join(timeout=10)

        # 只有补录一方落库：明细存在、批次恢复为 active（删除冲突不静默级联）。
        db = dbmod.SessionLocal()
        try:
            batch = db.get(Batch, batch_id)
            self.assertIsNotNone(batch, "批次不应被删除")
            self.assertEqual(batch.status, "active")
            count = (
                db.query(FeedingRecord)
                .filter(FeedingRecord.batch_id == batch_id)
                .count()
            )
            self.assertEqual(count, 1)
        finally:
            db.close()

        self.assertIn(("delete", "batch_has_children"), errors)
        self.assertNotIn(("create", "IntegrityError"), errors)

        # 失败事务后，新会话仍可正常服务（无脏事务污染）。
        db = dbmod.SessionLocal()
        try:
            self._insert_feeding(db, batch_id, "after")
            self.assertEqual(
                db.query(FeedingRecord)
                .filter(FeedingRecord.batch_id == batch_id)
                .count(),
                2,
            )
        finally:
            db.close()

    def test_deleting_window_reports_batch_deleting_to_competing_create(self):
        _, batch_id = self.make_pond_batch()
        # 第一阶段：删除方先持久化 deleting。
        db = dbmod.SessionLocal()
        try:
            service.mark_batch_deleting(db, batch_id)
        finally:
            db.close()

        # 竞争的补录请求拿到明确的“正在删除”冲突。
        db = dbmod.SessionLocal()
        try:
            with self.assertRaises(service.IntegrityConflict) as ctx:
                self._insert_feeding(db, batch_id)
            self.assertEqual(ctx.exception.code, service.BATCH_DELETING)
        finally:
            db.close()

        # 第二阶段：仍为空，删除方提交删除——只有清理一方落库。
        db = dbmod.SessionLocal()
        try:
            service.delete_batch(db, batch_id)
        finally:
            db.close()
        db = dbmod.SessionLocal()
        try:
            self.assertIsNone(db.get(Batch, batch_id))
            self.assertEqual(
                db.query(FeedingRecord)
                .filter(FeedingRecord.batch_id == batch_id)
                .count(),
                0,
            )
        finally:
            db.close()

    def test_crash_mid_delete_is_recovered_on_restart(self):
        _, batch_id = self.make_pond_batch()
        # 模拟删除流程在第一阶段提交后、第二阶段前崩溃：deleting 已落盘。
        db = dbmod.SessionLocal()
        try:
            service.mark_batch_deleting(db, batch_id)
        finally:
            db.close()

        self.restart_service()  # 启动对账

        r = self.client.get(f"/api/batches/{batch_id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "active")
        # 恢复后可继续挂明细
        r = self.client.post(
            "/api/feeding-records/",
            json={
                "batch_id": batch_id,
                "feeding_date": "2026-03-02",
                "feed_type": "A",
                "feed_quantity": 1.0,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
