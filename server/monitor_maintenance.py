#!/usr/bin/env python3
"""Consistent SQLite backup and bounded retention for the monitor service."""

from __future__ import annotations

import gzip
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


DB_PATH = Path(os.getenv("MONITOR_DB", "/opt/experiment-monitor/data/monitor.db"))
BACKUP_DIR = Path(os.getenv("MONITOR_BACKUP_DIR", "/opt/experiment-monitor/data/backups"))
KEEP_DAYS = max(1, int(os.getenv("MONITOR_BACKUP_KEEP_DAYS", "14")))
KEEP_COUNT = max(1, int(os.getenv("MONITOR_BACKUP_KEEP_COUNT", "14")))
RUN_RETENTION_DAYS = max(0, int(os.getenv("MONITOR_RUN_RETENTION_DAYS", "0")))


def backup_database() -> Path:
    if not DB_PATH.is_file():
        raise SystemExit(f"database not found: {DB_PATH}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = BACKUP_DIR / f"monitor-{stamp}.db.gz"
    with tempfile.TemporaryDirectory(dir=BACKUP_DIR) as temp_dir:
        temp_db = Path(temp_dir) / "monitor.db"
        source = sqlite3.connect(DB_PATH, timeout=30)
        target = sqlite3.connect(temp_db)
        try:
            source.execute("PRAGMA wal_checkpoint(PASSIVE)")
            source.backup(target)
        finally:
            target.close()
            source.close()
        temp_gzip = Path(temp_dir) / "monitor.db.gz"
        with temp_db.open("rb") as source_file, gzip.open(temp_gzip, "wb", compresslevel=6) as output:
            while chunk := source_file.read(1024 * 1024):
                output.write(chunk)
        os.replace(temp_gzip, final_path)
    return final_path


def prune_backups() -> int:
    backups = sorted(BACKUP_DIR.glob("monitor-*.db.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    removed = 0
    for index, path in enumerate(backups):
        if index >= KEEP_COUNT or path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def prune_runs() -> int:
    if RUN_RETENTION_DAYS <= 0:
        return 0
    cutoff = datetime.fromtimestamp(
        time.time() - RUN_RETENTION_DAYS * 86400, timezone.utc
    ).isoformat(timespec="seconds")
    connection = sqlite3.connect(DB_PATH, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        cursor = connection.execute(
            """DELETE FROM runs WHERE status IN ('completed', 'failed')
            AND ended_at IS NOT NULL AND ended_at < ?""",
            (cutoff,),
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def main():
    backup = backup_database()
    removed_backups = prune_backups()
    removed_runs = prune_runs()
    print(
        f"backup={backup} size={backup.stat().st_size} "
        f"removed_backups={removed_backups} removed_runs={removed_runs}"
    )


if __name__ == "__main__":
    main()
