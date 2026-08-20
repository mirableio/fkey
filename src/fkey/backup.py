"""Create and retain complete, SQLite-safe fkey backup sets."""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DAILY_RETENTION = 7
BACKUP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}\.\d{6}Z$")
TEMPORARY_NAME = re.compile(r"^\.\d{4}-\d{2}-\d{2}T\d{6}\.\d{6}Z\.tmp$")


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_db:
        with closing(sqlite3.connect(destination)) as destination_db:
            source_db.backup(destination_db)
    os.chmod(destination, 0o600)
    destination_uri = f"{destination.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(destination_uri, uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {source}")


def _user_databases(accounts_backup: Path, data_dir: Path) -> list[Path]:
    uri = f"{accounts_backup.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        user_ids = sorted(row[0] for row in db.execute("SELECT id FROM users"))
    databases = [data_dir / "users" / user_id / "db.sqlite" for user_id in user_ids]
    missing = [path for path in databases if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"User database not found: {missing[0]}")
    return databases


def _copy_snapshots(source_database: Path, destination_database: Path) -> None:
    source_dir = source_database.parent / "backups"
    if not source_dir.is_dir():
        return
    destination_dir = destination_database.parent / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for source in sorted(source_dir.glob("*.sqlite"), reverse=True):
        destination = destination_dir / source.name
        shutil.copy2(source, destination)
        os.chmod(destination, 0o600)


def _remove_stale_temporaries(backup_root: Path) -> int:
    stale = [
        path
        for path in backup_root.iterdir()
        if path.is_dir() and TEMPORARY_NAME.fullmatch(path.name)
    ]
    for path in stale:
        shutil.rmtree(path)
    return len(stale)


def _prune_backup_sets(backup_root: Path, keep: int) -> int:
    backup_sets = sorted(
        (
            path
            for path in backup_root.iterdir()
            if path.is_dir() and BACKUP_NAME.fullmatch(path.name)
        ),
        reverse=True,
    )
    for path in backup_sets[keep:]:
        shutil.rmtree(path)
    return len(backup_sets[keep:])


def run_backup(
    data_dir: Path,
    backup_root: Path,
    *,
    now: datetime | None = None,
    keep_daily: int = DAILY_RETENTION,
    once_per_day: bool = False,
) -> Path:
    accounts = data_dir / "accounts.sqlite"
    if not accounts.is_file():
        raise FileNotFoundError(f"Account database not found: {accounts}")

    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    daily_prefix = timestamp.strftime("%Y-%m-%dT")
    if once_per_day:
        existing = sorted(
            (
                path
                for path in backup_root.iterdir()
                if path.is_dir()
                and BACKUP_NAME.fullmatch(path.name)
                and path.name.startswith(daily_prefix)
            ),
            reverse=True,
        )
        if existing:
            print(f"Daily backup already exists: {existing[0]}")
            return existing[0]

    name = timestamp.strftime("%Y-%m-%dT%H%M%S.%fZ")
    destination = backup_root / name
    temporary = backup_root / f".{name}.tmp"
    if destination.exists() or temporary.exists():
        raise FileExistsError(f"Backup already exists for {name}")

    databases: list[Path] = []
    try:
        temporary.mkdir(mode=0o700)
        accounts_backup = temporary / "accounts.sqlite"
        _backup_database(accounts, accounts_backup)
        databases = _user_databases(accounts_backup, data_dir)
        for source in databases:
            relative = source.relative_to(data_dir)
            target = temporary / relative
            _backup_database(source, target)
            _copy_snapshots(source, target)
        temporary.rename(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    pruned_sets = _prune_backup_sets(backup_root, keep_daily)
    print(
        f"Created {destination} with {len(databases)} user databases; "
        f"pruned {pruned_sets} daily sets."
    )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once-per-day", action="store_true")
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("backup_root", type=Path)
    args = parser.parse_args()
    backup_root = args.backup_root.resolve()
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    with (backup_root / ".lock").open("w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _remove_stale_temporaries(backup_root)
        run_backup(
            args.data_dir.resolve(),
            backup_root,
            once_per_day=args.once_per_day,
        )


if __name__ == "__main__":
    main()
