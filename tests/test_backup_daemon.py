import asyncio
import os
import shutil
import unittest
from pathlib import Path
import io
import sqlite3
import tarfile
import aiosqlite

from src.utils.backup_daemon import TenantBackupDaemon, write_snapshot_archive


class TestTenantBackupDaemon(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_root = Path("data/test_backup_env")
        self.source_dir = self.test_root / "tenants"
        self.backup_dir = self.test_root / "backups"
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.tenant_id = "tenant_test_curtains_001"
        self.db_path = self.source_dir / f"{self.tenant_id}.sqlite"

        # Create a sample tenant SQLite database with WAL mode and data
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode = WAL;")
            await db.execute("CREATE TABLE test_data (id INT, text TEXT);")
            await db.execute("INSERT INTO test_data VALUES (1, 'initial configuration');")
            await db.commit()

        self.daemon = TenantBackupDaemon(
            source_dir=str(self.source_dir),
            backup_dir=str(self.backup_dir),
            retention_days=1,
        )

    async def asyncTearDown(self):
        if self.test_root.exists():
            shutil.rmtree(self.test_root)

    async def test_atomic_backup_execution(self):
        backup_path = await self.daemon.backup_single_tenant(self.tenant_id)
        self.assertIsNotNone(backup_path)
        self.assertTrue(backup_path.exists())

        # Verify integrity and content of the backed-up database
        async with aiosqlite.connect(backup_path) as db:
            cursor = await db.execute("SELECT text FROM test_data WHERE id = 1;")
            row = await cursor.fetchone()
            self.assertEqual(row[0], "initial configuration")

    async def test_backup_all_tenants(self):
        backups = await self.daemon.backup_all_tenants()
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].name.startswith(self.tenant_id))


    async def test_snapshot_archive_includes_uncheckpointed_data_and_config(self):
        """The off-server archive holds every tenant DB (including rows still only in the WAL) and tenants.yaml."""
        # Keep a writer open so the new row stays in the WAL file, as in the running app.
        writer = sqlite3.connect(self.db_path)
        writer.execute("PRAGMA wal_autocheckpoint=0;")
        writer.execute("INSERT INTO test_data VALUES (2, 'rule saved just now');")
        writer.commit()
        self.assertTrue(Path(f"{self.db_path}-wal").exists())

        config_file = self.test_root / "tenants.yaml"
        config_file.write_text("tenants: []\n")

        buffer = io.BytesIO()
        members = write_snapshot_archive(buffer, source_dir=str(self.source_dir), config_path=str(config_file))
        writer.close()

        self.assertEqual(members, [f"tenants/{self.tenant_id}.sqlite", "config/tenants.yaml"])

        restore_dir = self.test_root / "restore"
        buffer.seek(0)
        with tarfile.open(fileobj=buffer, mode="r:gz") as tar:
            tar.extractall(restore_dir, filter="data")

        restored = sqlite3.connect(restore_dir / "tenants" / f"{self.tenant_id}.sqlite")
        rows = restored.execute("SELECT id, text FROM test_data ORDER BY id;").fetchall()
        integrity = restored.execute("PRAGMA integrity_check;").fetchone()[0]
        restored.close()
        self.assertEqual(rows, [(1, "initial configuration"), (2, "rule saved just now")])
        self.assertEqual(integrity, "ok")
        self.assertEqual((restore_dir / "config" / "tenants.yaml").read_text(), "tenants: []\n")


if __name__ == "__main__":
    unittest.main()