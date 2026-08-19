from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fkey.engine import TasteDB
from seed_support import load_seed


FIXTURE = Path(__file__).parent / "fixtures" / "seed.yaml"


class SeedFixtureTest(unittest.TestCase):
    def test_loads_generic_collections_records_and_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = TasteDB(Path(temporary) / "db.sqlite")

            result = load_seed(database, FIXTURE)

            self.assertEqual(
                result, {"collections": 1, "records": 3, "links": 2}
            )
            restaurant = database.get_record("restaurants", "taizu")
            self.assertEqual(restaurant["record"]["extra"], {"occasion": "date night"})
            self.assertEqual(restaurant["links"][0]["from_id"], "anna")

            movie = database.get_record("movies", "inception-2010")
            self.assertEqual(movie["links"][0]["rating"], 9)
            self.assertEqual(
                movie["links"][0]["props"],
                {"watched_with": "partner", "would_rewatch": True},
            )


if __name__ == "__main__":
    unittest.main()
