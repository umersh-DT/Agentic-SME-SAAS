import asyncio
import datetime
import logging
import os
from pathlib import Path
from typing import List, Optional
import aiosqlite

logger = logging.getLogger("backup_daemon")


class TenantBackupDaemon:
    """Creates non-blocking atomic backups of tenant SQLite databases in WAL mode."""

    def __init__(
        self,
        source_dir: str = "/app/data/tenants",
        backup_dir: str = "/app/data/backups",
        retention_days: int = 7,
    ):
        self.source_dir = Path(source_dir)
        self.backup_dir = Path(backup_dir)
        self.retention_days = retention_days

    def _ensure_directories(self) -> None:
        self.source_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    async def backup_single_tenant(self, tenant_id: str) -> Optional[Path]:
        """Performs an atomic online backup of a tenant database using SQLite Online Backup API."""
        src_db_path = self.source_dir / f"{tenant_id}.sqlite"
        if not src_db_path.exists():
            logger.warning(f"[BACKUP SKIP] Database not found for tenant: {tenant_id}")
            return None

        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        tenant_backup_folder = self.backup_dir / tenant_id
        tenant_backup_folder.mkdir(parents=True, exist_ok=True)
        dest_db_path = tenant_backup_folder / f"{tenant_id}_{timestamp}.sqlite"

        try:
            async with aiosqlite.connect(src_db_path) as src_conn:
                async with aiosqlite.connect(dest_db_path) as dst_conn:
                    # Leverage the SQLite online backup API to copy pages safely under WAL mode
                    await src_conn.backup(dst_conn._conn)

            logger.info(f"[BACKUP SUCCESS] Backed up {tenant_id} -> {dest_db_path.name}")
            return dest_db_path
        except Exception as exc:
            logger.error(f"[BACKUP ERROR] Failed to backup {tenant_id}: {exc}")
            if dest_db_path.exists():
                dest_db_path.unlink()
            return None

    async def backup_all_tenants(self) -> List[Path]:
        """Discovers all tenant databases in source_dir and executes atomic snapshots."""
        self._ensure_directories()
        successful_backups: List[Path] = []
        db_files = list(self.source_dir.glob("*.sqlite"))

        for db_file in db_files:
            tenant_id = db_file.stem
            backup_path = await self.backup_single_tenant(tenant_id)
            if backup_path:
                successful_backups.append(backup_path)

        self.purge_expired_backups()
        return successful_backups

    def purge_expired_backups(self) -> int:
        """Removes local backup files older than retention_days."""
        purged_count = 0
        cutoff_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=self.retention_days
        )

        for tenant_dir in self.backup_dir.iterdir():
            if tenant_dir.is_dir():
                for backup_file in tenant_dir.glob("*.sqlite"):
                    file_mtime = datetime.datetime.fromtimestamp(
                        backup_file.stat().st_mtime, tz=datetime.timezone.utc
                    )
                    if file_mtime < cutoff_time:
                        backup_file.unlink()
                        purged_count += 1
                        logger.info(f"[BACKUP PURGE] Removed expired backup: {backup_file.name}")

        return purged_count


if __name__ == "__main__":
    # Test backup daemon execution locally
    async def _test():
        daemon = TenantBackupDaemon(source_dir="data/tenants", backup_dir="data/backups")
        backups = await daemon.backup_all_tenants()
        print(f"Completed {len(backups)} backups.")

    asyncio.run(_test())