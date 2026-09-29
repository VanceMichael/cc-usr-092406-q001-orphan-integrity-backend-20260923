"""清理批次与现场补录竞争：只有一方提交，负方拿稳定冲突，绝不留孤儿。"""

import datetime
import unittest

from sqlalchemy import text

from support import new_db_file, fresh_engine, run_concurrent
from app import models, services
from app.database import mark_write_intent
from app.errors import AppError

DATE = datetime.date(2026, 1, 1)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.path = new_db_file("concurrency.db")
        self.engine, self.Session = fresh_engine(self.path)
        s = self.Session()
        mark_write_intent()
        s.add(models.Pond(name="P", area=10, water_depth=2))
        s.commit()
        s.close()
        self._serial = 0

    def _new_batch(self, with_child=False):
        self._serial += 1
        s = self.Session()
        mark_write_intent()
        b = models.Batch(
            batch_number=f"B{self._serial}", pond_id=1, species="鲈鱼",
            stocking_date=DATE)
        s.add(b)
        s.commit()
        bid = b.id
        if with_child:
            s.add(models.FeedingRecord(
                batch_id=bid, feeding_date=datetime.date(2026, 2, 1),
                feed_type="颗粒", feed_quantity=1.0))
            s.commit()
        s.close()
        return bid

    def _create_feeding(self, bid):
        s = self.Session()
        try:
            services.create_child(s, models.FeedingRecord, {
                "batch_id": bid,
                "feeding_date": datetime.date(2026, 3, 1),
                "feed_type": "配合料", "feed_quantity": 2.0})
            return "ok"
        except AppError as exc:
            return exc.code
        finally:
            s.close()

    def _delete_batch(self, bid):
        s = self.Session()
        try:
            services.delete_batch(s, bid)
            return "ok"
        except AppError as exc:
            return exc.code
        finally:
            s.close()

    def _state(self, bid):
        s = self.Session()
        batch = s.get(models.Batch, bid)
        children = s.query(models.FeedingRecord).filter_by(batch_id=bid).count()
        orphans = s.execute(text(
            "SELECT COUNT(*) FROM feeding_records f WHERE NOT EXISTS "
            "(SELECT 1 FROM batches b WHERE b.id=f.batch_id)")).scalar()
        s.close()
        return (None if batch is None else batch.status), children, orphans

    def test_exactly_one_side_commits_under_simultaneous_race(self):
        # 同屏障同时起跑多轮：每轮要么删除成、补录败，要么补录成、删除败。
        winners = {"delete": 0, "create": 0}
        for _ in range(15):
            bid = self._new_batch()
            results = run_concurrent(
                ("delete", lambda: self._delete_batch(bid)),
                ("create", lambda: self._create_feeding(bid)),
            )
            # 两个工作函数都已把业务异常转成 code 字符串。
            delete_outcome = results["delete"][1]
            create_outcome = results["create"][1]
            status, children, orphans = self._state(bid)

            self.assertEqual(orphans, 0)
            delete_won = delete_outcome == "ok"
            create_won = create_outcome == "ok"
            self.assertNotEqual(delete_won, create_won,
                                f"必须恰好一方提交: {results}")
            if delete_won:
                self.assertIn(create_outcome,
                              ("batch_not_found", "batch_deleting"))
                self.assertIsNone(status)
                self.assertEqual(children, 0)
                winners["delete"] += 1
            else:
                self.assertEqual(delete_outcome, "batch_has_children")
                self.assertEqual(status, "active")
                self.assertEqual(children, 1)
                winners["create"] += 1
        # 同屏障下补录是单条短事务、删除是两阶段，通常先拿锁；
        # 反向结局（删除先持锁）由 test_when_delete_commits_first_* 确定性覆盖。
        # 这里只要求每轮都恰好一方提交，且总数与轮数一致。
        self.assertEqual(winners["delete"] + winners["create"], 15)

    def test_when_delete_commits_first_create_is_rejected(self):
        # 删除方先拿锁并完成：后到的补录必须失败，批次已删、零孤儿。
        bid = self._new_batch()
        import threading

        create_result = {}

        def creator():
            create_result["v"] = self._create_feeding(bid)

        delete_outcome = self._delete_batch(bid)  # 先提交删除
        t = threading.Thread(target=creator)
        t.start()
        t.join(10)

        self.assertEqual(delete_outcome, "ok")
        self.assertEqual(create_result["v"], "batch_not_found")
        status, children, orphans = self._state(bid)
        self.assertIsNone(status)
        self.assertEqual(children, 0)
        self.assertEqual(orphans, 0)

    def test_when_create_commits_first_delete_gets_stable_conflict(self):
        # 补录先提交：随后的删除必须稳定冲突，批次保留、不级联。
        bid = self._new_batch()
        create_outcome = self._create_feeding(bid)  # 先补录
        delete_outcome = self._delete_batch(bid)

        self.assertEqual(create_outcome, "ok")
        self.assertEqual(delete_outcome, "batch_has_children")
        status, children, orphans = self._state(bid)
        self.assertEqual(status, "active")
        self.assertEqual(children, 1)
        self.assertEqual(orphans, 0)

    def test_populated_batch_delete_always_loses_even_under_race(self):
        # 批次本来就有明细：无论怎样竞争，删除都必须冲突，明细一条不丢。
        for _ in range(10):
            bid = self._new_batch(with_child=True)
            results = run_concurrent(
                ("delete", lambda: self._delete_batch(bid)),
                ("create", lambda: self._create_feeding(bid)),
            )
            # 删除绝不允许成功
            self.assertEqual(results["delete"][1], "batch_has_children", results)
            status, children, orphans = self._state(bid)
            self.assertEqual(status, "active")
            self.assertGreaterEqual(children, 1)
            self.assertEqual(orphans, 0)

    def test_no_deleting_status_left_after_finished_delete_attempts(self):
        # 所有竞争结束后不得残留 deleting（崩溃恢复另由迁移测试覆盖）。
        for _ in range(5):
            bid = self._new_batch()
            run_concurrent(
                ("delete", lambda: self._delete_batch(bid)),
                ("create", lambda: self._create_feeding(bid)),
            )
        s = self.Session()
        n = s.query(models.Batch).filter(
            models.Batch.status == "deleting").count()
        s.close()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
