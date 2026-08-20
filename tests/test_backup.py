from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fkey.backup import _remove_stale_temporaries, run_backup


class BackupTest(unittest.TestCase):
    def test_stale_temporary_sets_are_removed_selectively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backup_root = Path(temporary)
            stale = backup_root / ".2026-02-01T000000.000000Z.tmp"
            unrelated = backup_root / ".notes.tmp"
            stale.mkdir()
            unrelated.mkdir()

            self.assertEqual(_remove_stale_temporaries(backup_root), 1)
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())

    def test_failed_backup_leaves_no_partial_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "accounts.sqlite").write_bytes(b"not sqlite")
            backup_root = root / "backups"

            with self.assertRaises(sqlite3.DatabaseError):
                run_backup(data, backup_root)

            self.assertEqual(list(backup_root.iterdir()), [])

    def test_missing_user_database_fails_the_complete_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            with closing(sqlite3.connect(data / "accounts.sqlite")) as db:
                db.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO users VALUES (?)", ("a" * 32,))
                db.commit()
            backup_root = root / "backups"

            with self.assertRaises(FileNotFoundError):
                run_backup(data, backup_root)

            self.assertEqual(list(backup_root.iterdir()), [])

    def test_complete_backup_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            user_ids = ("a" * 32, "b" * 32)
            users = [data / "users" / user_id for user_id in user_ids]
            for user in users:
                user.mkdir(parents=True)

            with closing(sqlite3.connect(data / "accounts.sqlite")) as db:
                db.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
                db.executemany(
                    "INSERT INTO users VALUES (?)", [(item,) for item in user_ids]
                )
                db.commit()
            for user, title in zip(users, ("Arrival", "Heat"), strict=True):
                with closing(sqlite3.connect(user / "db.sqlite")) as db:
                    db.execute("CREATE TABLE movies (title TEXT)")
                    db.execute("INSERT INTO movies VALUES (?)", (title,))
                    db.commit()

            snapshots = users[0] / "backups"
            snapshots.mkdir()
            for index in range(15):
                shutil.copy2(
                    users[0] / "db.sqlite",
                    snapshots / f"2026-01-{index + 1:02d}T000000--migration.sqlite",
                )

            backup_root = root / "backups"
            for day in range(1, 9):
                run_backup(
                    data,
                    backup_root,
                    now=datetime(2026, 2, day, tzinfo=timezone.utc),
                )

            backup_sets = sorted(backup_root.iterdir())
            self.assertEqual(len(backup_sets), 7)
            self.assertTrue((backup_sets[-1] / "accounts.sqlite").is_file())
            copied_databases = [
                backup_sets[-1] / "users" / user_id / "db.sqlite"
                for user_id in user_ids
            ]
            for copied_database, title in zip(
                copied_databases, ("Arrival", "Heat"), strict=True
            ):
                with closing(sqlite3.connect(copied_database)) as db:
                    self.assertEqual(
                        db.execute("SELECT title FROM movies").fetchone()[0], title
                    )
            self.assertEqual(
                len(list((copied_databases[0].parent / "backups").glob("*.sqlite"))),
                15,
            )
            self.assertEqual(len(list(snapshots.glob("*.sqlite"))), 15)

            repeated = run_backup(
                data,
                backup_root,
                now=datetime(2026, 2, 8, 12, tzinfo=timezone.utc),
                once_per_day=True,
            )
            self.assertEqual(repeated, backup_sets[-1])
            self.assertEqual(len(list(backup_root.iterdir())), 7)


if __name__ == "__main__":
    unittest.main()
