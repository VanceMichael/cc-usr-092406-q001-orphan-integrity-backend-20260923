"""测试辅助：为每个用例配置独立临时 SQLite，并模拟服务重启。"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 首次导入 app.main 之前指到一个占位临时库，避免在仓库目录生成 aquaculture.db。
_TMPDIR = tempfile.mkdtemp(prefix="q001-tests-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDIR}/bootstrap.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import database as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.migration import run_migrations  # noqa: E402
from app.integrity import reconcile_pending_deletions  # noqa: E402


class FreshDatabaseCase:
    """混入：setUp 绑定全新临时库并完成建表/迁移，self.client 立即可用。"""

    def setUp(self):  # noqa: N802 (unittest 命名)
        fd, self.db_path = tempfile.mkstemp(suffix=".db", dir=_TMPDIR)
        os.close(fd)
        os.unlink(self.db_path)
        self.db_url = f"sqlite:///{self.db_path}"
        self._bring_up(self.db_url)
        self.client = TestClient(app)

    def _bring_up(self, url):
        # configure 只更换引擎，Base 标识不变；路由通过模块全局动态取会话。
        dbmod.configure(url)
        Base.metadata.create_all(bind=dbmod.engine)
        run_migrations(dbmod.engine)
        session = dbmod.SessionLocal()
        try:
            reconcile_pending_deletions(session)
        finally:
            session.close()

    def restart_service(self):
        """模拟进程重启：重新配置同一文件并重跑启动序列。"""
        self._bring_up(self.db_url)
        self.client = TestClient(app)

    def raw_external_connection(self, foreign_keys: bool = False):
        """模拟外部程序直连（默认按 SQLite 默认关闭外键强制）。"""
        con = sqlite3.connect(self.db_path)
        con.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        return con

    def make_pond_batch(self, batch_number="B-001", status="active"):
        r = self.client.post(
            "/api/ponds/",
            json={"name": f"P-{batch_number}", "area": 10, "water_depth": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        pond_id = r.json()["id"]
        r = self.client.post(
            "/api/batches/",
            json={
                "batch_number": batch_number,
                "pond_id": pond_id,
                "species": "鲫鱼",
                "stocking_date": "2026-03-01",
                "status": status,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        return pond_id, r.json()["id"]


class OldDatabaseCase(FreshDatabaseCase):
    """混入：setUp 先落地一份升级前的旧库（无 ON DELETE、含历史孤儿）。"""

    def setUp(self):  # noqa: N802
        from olddb_fixture import build_old_db

        fd, self.db_path = tempfile.mkstemp(suffix=".db", dir=_TMPDIR)
        os.close(fd)
        os.unlink(self.db_path)
        self.db_url = f"sqlite:///{self.db_path}"
        build_old_db(self.db_path)
        self._bring_up(self.db_url)
        self.client = TestClient(app)
