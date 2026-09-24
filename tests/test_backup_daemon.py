import asyncio
import os
import shutil
import unittest
from pathlib import Path
import aiosqlite

from src.utils.backup_daemon import TenantBackupDaemon


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


if __name__ == "__main__":
    unittest.main()