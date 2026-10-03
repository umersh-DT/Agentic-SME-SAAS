import argparse
import asyncio
import datetime
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from typing import BinaryIO, List, Optional
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


def _snapshot_sqlite(src_path: Path, dest_path: Path) -> None:
    """Copies a live WAL-mode database with the SQLite online backup API and checks the copy."""
    with sqlite3.connect(src_path, timeout=30.0) as src_conn, sqlite3.connect(dest_path) as dst_conn:
        src_conn.backup(dst_conn)
        result = dst_conn.execute("PRAGMA integrity_check;").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"Integrity check failed for snapshot of {src_path.name}: {result}")


def write_snapshot_archive(
    output: BinaryIO,
    source_dir: str = "/app/data/tenants",
    config_path: Optional[str] = None,
) -> List[str]:
    """Writes a gzip tar archive of consistent snapshots of every tenant database to `output`.

    Layout: tenants/<tenant_id>.sqlite and config/tenants.yaml. Returns the archived member names.
    Raises if any database cannot be snapshotted, so a partial backup is never reported as success.
    """
    src = Path(source_dir)
    members: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        (staging / "tenants").mkdir()
        for db_file in sorted(src.glob("*.sqlite")):
            _snapshot_sqlite(db_file, staging / "tenants" / db_file.name)
            members.append(f"tenants/{db_file.name}")

        cfg = Path(config_path or os.environ.get("TENANTS_CONFIG_PATH", "config/tenants.yaml"))
        if cfg.exists():
            (staging / "config").mkdir()
            shutil.copy2(cfg, staging / "config" / "tenants.yaml")
            members.append("config/tenants.yaml")

        with tarfile.open(fileobj=output, mode="w|gz") as tar:
            for name in members:
                tar.add(staging / name, arcname=name)
    return members


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot all tenant databases.")
    parser.add_argument(
        "--archive",
        metavar="PATH",
        help="Write a .tar.gz of consistent snapshots to PATH ('-' for stdout, used by scripts/backup.sh).",
    )
    parser.add_argument("--source-dir", default=os.environ.get("TENANTS_DATA_DIR", "/app/data/tenants"))
    args = parser.parse_args()

    if args.archive:
        if args.archive == "-":
            members = write_snapshot_archive(sys.stdout.buffer, source_dir=args.source_dir)
            sys.stdout.buffer.flush()
        else:
            with open(args.archive, "wb") as f:
                members = write_snapshot_archive(f, source_dir=args.source_dir)
        print(f"Archived {len(members)} files: {', '.join(members)}", file=sys.stderr)
        return

    daemon = TenantBackupDaemon(source_dir=args.source_dir)
    backups = asyncio.run(daemon.backup_all_tenants())
    print(f"Completed {len(backups)} backups.", file=sys.stderr)


if __name__ == "__main__":
    main()