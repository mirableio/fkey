from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fkey.engine import TasteDB, slugify
from fkey.migrations import SNAPSHOT_RETENTION


class TasteDBTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "db.sqlite"
        self.db = TasteDB(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_bootstrap_seeds_self_and_standard_collections(self) -> None:
        schema = self.db.describe_schema()
        names = {item["name"] for item in schema["collections"]}
        self.assertEqual(
            names,
            {
                "books",
                "cocktails",
                "movies",
                "music",
                "people",
                "profiles",
                "shows",
                "wines",
            },
        )
        self.assertEqual(schema["profiles"][0]["id"], "self")

    def test_record_crud_requires_explicit_extra(self) -> None:
        with self.assertRaisesRegex(ValueError, "Did you mean 'title'"):
            self.db.add_record("movies", {"tittle": "Arrival"})

        movie = self.db.add_record(
            "movies",
            {"title": "Arrival", "year": 2016},
            {"format": "IMAX", "mood": "hopeful"},
        )
        self.assertEqual(movie["id"], "arrival-2016")
        self.assertEqual(movie["extra"]["format"], "IMAX")

        updated = self.db.update_record(
            "movies",
            movie["id"],
            {"year": 2017},
            {"format": None, "rewatch": True},
        )
        self.assertEqual(updated["year"], 2017)
        self.assertNotIn("format", updated["extra"])
        self.assertEqual(updated["extra"]["mood"], "hopeful")
        self.assertTrue(updated["extra"]["rewatch"])

    def test_link_upsert_keeps_first_and_latest_activity(self) -> None:
        movie = self.db.add_record("movies", {"title": "Dune", "year": 2021})
        first = self.db.add_link(
            "profiles",
            "self",
            "movies",
            movie["id"],
            values={"rating": 9, "at": "2026-08-10", "note": "Loved it"},
            props={"format": "IMAX"},
        )
        self.assertEqual(first["first_at"], "2026-08-10")

        updated = self.db.add_link(
            "profiles",
            "self",
            "movies",
            movie["id"],
            values={"at": "2026-08-18"},
            props={"format": None, "with": "family"},
        )
        self.assertEqual(updated["first_at"], "2026-08-10")
        self.assertEqual(updated["last_at"], "2026-08-18")
        self.assertEqual(updated["rating"], 9)
        self.assertEqual(updated["note"], "Loved it")
        self.assertEqual(updated["props"], {"with": "family"})

        backdated = self.db.add_link(
            "profiles",
            "self",
            "movies",
            movie["id"],
            values={"at": "2026-08-01"},
        )
        self.assertEqual(backdated["first_at"], "2026-08-01")
        self.assertEqual(backdated["last_at"], "2026-08-18")

        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            self.db.add_link(
                "profiles",
                "self",
                "movies",
                movie["id"],
                values={"at": 20260820},
            )

    def test_link_validation_and_rating_scale(self) -> None:
        movie = self.db.add_record("movies", {"title": "Dune", "year": 2021})
        with self.assertRaisesRegex(ValueError, "profile-to-non-profile"):
            self.db.add_link("movies", movie["id"], "profiles", "self")
        with self.assertRaisesRegex(ValueError, "Did you mean 'pairs_with'"):
            self.db.add_link(
                "movies", movie["id"], "profiles", "self", kind="pair_with"
            )
        with self.assertRaisesRegex(ValueError, "between 1 and 10"):
            self.db.add_link(
                "profiles", "self", "movies", movie["id"], values={"rating": 11}
            )

        self.db.migrate(
            "use a five point book scale",
            [
                """
                INSERT INTO _meta
                    (scope, collection, name, description, value_json)
                VALUES
                    ('rating_scale', 'books', 'default', 'Five point scale.',
                     '{"min":1,"max":5}')
                """
            ],
        )
        book = self.db.add_record("books", {"title": "Piranesi", "year": 2020})
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            self.db.add_link(
                "profiles", "self", "books", book["id"], values={"rating": 6}
            )
        convention = self.db.describe_schema()["meta_table"][
            "rating_scale_convention"
        ]
        self.assertEqual(convention["name"], "default")

    def test_movie_night_query(self) -> None:
        anna = self.db.add_record("profiles", {"name": "Anna", "relation": "wife"})
        dad = self.db.add_record("profiles", {"name": "Dad", "relation": "father"})
        dune = self.db.add_record("movies", {"title": "Dune", "year": 2021})
        arrival = self.db.add_record("movies", {"title": "Arrival", "year": 2016})
        self.db.add_link(
            "profiles", anna["id"], "movies", dune["id"], values={"rating": 9}
        )
        self.db.add_link(
            "profiles", anna["id"], "movies", arrival["id"], values={"rating": 8}
        )
        self.db.add_link(
            "profiles", dad["id"], "movies", arrival["id"], values={"rating": 7}
        )

        result = self.db.query(
            """
            SELECT m.title
            FROM movies AS m
            JOIN links AS anna
              ON anna.to_collection = 'movies'
             AND anna.to_id = m.id
             AND anna.from_collection = 'profiles'
             AND anna.from_id = ?
             AND anna.kind = ''
            WHERE anna.rating >= 8
              AND NOT EXISTS (
                  SELECT 1 FROM links AS dad
                  WHERE dad.from_collection = 'profiles'
                    AND dad.from_id = ?
                    AND dad.to_collection = 'movies'
                    AND dad.to_id = m.id
                    AND dad.kind = ''
              )
            """,
            [anna["id"], dad["id"]],
        )
        self.assertEqual(result["rows"], [{"title": "Dune"}])

    def test_create_collection(self) -> None:
        schema = self.db.create_collection(
            "restaurants",
            "Restaurants worth remembering.",
            [
                {"name": "name", "type": "text", "required": True},
                {"name": "city", "type": "text"},
            ],
        )
        self.assertEqual(schema["name"], "restaurants")
        restaurant = self.db.add_record(
            "restaurants", {"name": "Taizu", "city": "Tel Aviv"}
        )
        self.assertEqual(restaurant["id"], "taizu")

    def test_seed_collections_are_not_recreated_when_reopening(self) -> None:
        self.db.migrate(
            "remove unused cocktails",
            [
                """
                INSERT INTO _meta
                    (scope, collection, name, description, value_json)
                VALUES
                    ('rating_scale', 'cocktails', 'default', 'Cocktail scale.',
                     '{"min":1,"max":5}')
                """,
                "DROP TABLE cocktails",
            ],
        )

        reopened = TasteDB(self.path)

        self.assertNotIn("cocktails", reopened.collection_names())
        metadata = reopened.query(
            "SELECT scope FROM _meta WHERE collection = 'cocktails'"
        )
        self.assertEqual(metadata["rows"], [])
        global_scale = reopened.query(
            """
            SELECT name FROM _meta
            WHERE scope = 'rating_scale' AND collection = ''
            """
        )
        self.assertEqual(global_scale["rows"], [{"name": "default"}])

    def test_slugify_preserves_human_readable_unicode(self) -> None:
        self.assertEqual(slugify("Amélie"), "amélie")
        self.assertEqual(slugify("אמלי"), "אמלי")
        self.assertEqual(slugify("東京"), "東京")
        self.assertEqual(slugify("がっこう"), "がっこう")
        explicit = self.db.add_record(
            "movies", {"title": "Амели", "year": 2001}, record_id="амели-2001"
        )
        self.assertEqual(explicit["id"], "амели-2001")
        with self.assertRaisesRegex(ValueError, "canonical lowercase slug"):
            self.db.add_record(
                "movies", {"title": "Other"}, record_id="not_canonical"
            )

    def test_query_disambiguates_duplicate_column_names(self) -> None:
        result = self.db.query("SELECT 1 AS value, 2 AS value")

        self.assertEqual(result["columns"], ["value", "value_2"])
        self.assertEqual(result["rows"], [{"value": 1, "value_2": 2}])
        with self.assertRaisesRegex(ValueError, "SELECT or WITH"):
            self.db.query("DELETE FROM movies")

    def test_failed_migration_cannot_commit_early(self) -> None:
        movie = self.db.add_record("movies", {"title": "Arrival", "year": 2016})

        with self.assertRaisesRegex(ValueError, "may not manage transactions"):
            self.db.migrate(
                "attempt explicit commit",
                [
                    "UPDATE movies SET year = 2017 WHERE id = 'arrival-2016'",
                    "COMMIT",
                    "THIS IS INVALID SQL",
                ],
            )

        stored = self.db.get_record("movies", movie["id"])["record"]
        self.assertEqual(stored["year"], 2016)

    def test_migration_cannot_remove_required_structures(self) -> None:
        with self.assertRaisesRegex(ValueError, "required table 'profiles'"):
            self.db.migrate("remove profiles", ["DROP TABLE profiles"])

        self.assertEqual(self.db.get_record("profiles", "self")["record"]["id"], "self")
        self.assertEqual(self.db.describe_schema()["recent_migrations"], [])

    def test_migration_is_confined_and_preserves_history(self) -> None:
        side_database = Path(self.temporary.name) / "side.sqlite"
        escaped_path = str(side_database).replace("'", "''")
        with self.assertRaisesRegex(ValueError, "ATTACH is not allowed"):
            self.db.migrate(
                "escape the database",
                [f"ATTACH DATABASE '{escaped_path}' AS side"],
            )
        self.assertFalse(side_database.exists())

        with self.assertRaisesRegex(ValueError, "PRAGMA writable_schema"):
            self.db.migrate("change writable schema", ["PRAGMA writable_schema=ON"])
        with self.assertRaisesRegex(ValueError, "PRAGMA schema_version"):
            self.db.migrate("forge schema version", ["PRAGMA schema_version=99"])
        with self.assertRaisesRegex(ValueError, "cannot VACUUM"):
            self.db.migrate("vacuum inside migration", ["VACUUM"])
        with self.assertRaisesRegex(ValueError, "load_extension is not allowed"):
            self.db.migrate(
                "load an extension", ["SELECT load_extension('anything')"]
            )

        first = self.db.migrate("record one migration", ["SELECT 1"])
        with self.assertRaisesRegex(ValueError, "Writes to _migrations"):
            self.db.migrate("erase history", ["DELETE FROM _migrations"])
        migrations = self.db.describe_schema()["recent_migrations"]
        self.assertEqual([row["id"] for row in migrations], [first["migration_id"]])

    def test_migration_can_defer_foreign_keys(self) -> None:
        result = self.db.migrate(
            "reorder foreign-key inserts",
            [
                "CREATE TABLE _parents (id INTEGER PRIMARY KEY)",
                """
                CREATE TABLE _children (
                    parent_id INTEGER NOT NULL REFERENCES _parents(id)
                )
                """,
                "PRAGMA defer_foreign_keys=ON",
                "INSERT INTO _children (parent_id) VALUES (1)",
                "INSERT INTO _parents (id) VALUES (1)",
            ],
        )

        self.assertEqual(result["migration_id"], 1)
        self.assertEqual(
            self.db.query("SELECT parent_id FROM _children")["rows"],
            [{"parent_id": 1}],
        )

    def test_orphaned_link_rolls_migration_back(self) -> None:
        movie = self.db.add_record("movies", {"title": "Arrival", "year": 2016})
        self.db.add_link("profiles", "self", "movies", movie["id"])

        with self.assertRaisesRegex(ValueError, "missing record movies/missing"):
            self.db.migrate(
                "orphan a link",
                ["UPDATE links SET to_id = 'missing' WHERE to_collection = 'movies'"],
            )

        stored = self.db.get_record("movies", movie["id"])
        self.assertEqual(stored["links"][0]["to_id"], movie["id"])

    def test_invalid_snapshot_does_not_replace_live_database(self) -> None:
        movie = self.db.add_record("movies", {"title": "Arrival", "year": 2016})
        corrupt = self.db.backup_dir / "2099-01-01T000000000000--manual--bad.sqlite"
        corrupt.write_bytes(b"not a sqlite database")

        with self.assertRaisesRegex(ValueError, "live database was not changed"):
            self.db.restore_snapshot(corrupt.name)

        stored = self.db.get_record("movies", movie["id"])["record"]
        self.assertEqual(stored["title"], "Arrival")
        self.assertFalse(any(
            item["trigger"] == "restore-safety" for item in self.db.list_snapshots()
        ))

    def test_snapshot_retention_and_oldest_restore(self) -> None:
        created = [
            self.db.snapshot("manual", str(index))
            for index in range(SNAPSHOT_RETENTION + 2)
        ]

        snapshots = self.db.list_snapshots()
        self.assertEqual(len(snapshots), SNAPSHOT_RETENTION)
        self.assertFalse((self.db.backup_dir / created[0]).exists())
        self.assertFalse((self.db.backup_dir / created[1]).exists())

        oldest = snapshots[-1]["file"]
        restored = self.db.restore_snapshot(oldest)
        self.assertEqual(restored["restored"], oldest)
        self.assertEqual(len(self.db.list_snapshots()), SNAPSHOT_RETENTION)

    def test_migration_snapshot_and_restore(self) -> None:
        wine = self.db.add_record(
            "wines", {"name": "Example", "vintage": 2022}, {"grape": "Syrah"}
        )
        result = self.db.migrate(
            "promote grape on wines",
            [
                "ALTER TABLE wines ADD COLUMN grape TEXT",
                "UPDATE wines SET grape = json_extract(extra, '$.grape')",
                """
                INSERT INTO _meta (scope, collection, name, description)
                VALUES ('field', 'wines', 'grape', 'Grape variety.')
                """,
            ],
        )
        promoted = self.db.get_record("wines", wine["id"])["record"]
        self.assertEqual(promoted["grape"], "Syrah")
        self.assertTrue(result["snapshot"].endswith(".sqlite"))

        restored = self.db.restore_snapshot(result["snapshot"])
        restored_schema = self.db.describe_schema("wines")
        self.assertNotIn("grape", {field["name"] for field in restored_schema["fields"]})

        self.db.restore_snapshot(restored["safety_snapshot"])
        restored_again = self.db.get_record("wines", wine["id"])["record"]
        self.assertEqual(restored_again["grape"], "Syrah")
        self.assertGreaterEqual(len(self.db.list_snapshots()), 3)


if __name__ == "__main__":
    unittest.main()
