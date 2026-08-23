from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp.client import Client

from fkey.server import configure, engine, http_app, mcp


class MCPModeTest(unittest.TestCase):
    def test_http_mode_never_falls_back_to_local_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local_path = Path(temporary) / "local.sqlite"
            configure(local_path)
            self.assertEqual(engine().path, local_path.resolve())
            try:
                http_app()
                with self.assertRaisesRegex(PermissionError, "identity is required"):
                    engine()
            finally:
                configure(local_path)


class MCPProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_static_tool_surface_and_record_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary) / "db.sqlite")
            async with Client(mcp) as client:
                result = await client.list_tools()
                names = {tool.name for tool in result.tools}
                self.assertEqual(
                    names,
                    {
                        "describe_schema",
                        "add_record",
                        "add_multiple_records",
                        "get_record",
                        "update_record",
                        "delete_record",
                        "find_records",
                        "add_link",
                        "remove_link",
                        "query",
                        "create_collection",
                        "migrate",
                        "restore_snapshot",
                        "list_snapshots",
                    },
                )

                call = await client.call_tool(
                    "add_record",
                    {
                        "collection": "movies",
                        "values": {"title": "Arrival", "year": 2016},
                    },
                )
                self.assertFalse(call.is_error)
                self.assertEqual(call.structured_content["id"], "arrival-2016")

                compact = await client.call_tool(
                    "add_record",
                    {
                        "collection": "movies",
                        "values": {"title": "Dune", "year": 2021},
                        "minimal": True,
                    },
                )
                self.assertEqual(compact.structured_content, {"id": "dune-2021"})

                compact_link = await client.call_tool(
                    "add_link",
                    {
                        "from_collection": "profiles",
                        "from_id": "self",
                        "to_collection": "movies",
                        "to_id": "dune-2021",
                        "values": {"rating": 9},
                        "minimal": True,
                    },
                )
                self.assertEqual(
                    compact_link.structured_content,
                    {
                        "from_collection": "profiles",
                        "from_id": "self",
                        "to_collection": "movies",
                        "to_id": "dune-2021",
                        "kind": None,
                    },
                )

                batch = await client.call_tool(
                    "add_multiple_records",
                    {
                        "collection": "wines",
                        "records": [
                            {
                                "values": {"name": "Red", "vintage": 2020},
                                "profile_link": {
                                    "values": {"rating": 8, "at": "2026-08-20"}
                                },
                            },
                            {"values": {"name": "White", "vintage": 2021}},
                        ],
                    },
                )
                self.assertEqual(
                    batch.structured_content,
                    {"items": [{"id": "red-2020"}, {"id": "white-2021"}]},
                )

                duplicate = await client.call_tool(
                    "add_multiple_records",
                    {
                        "collection": "wines",
                        "records": [{"values": {"name": "Red", "vintage": 2020}}],
                    },
                )
                self.assertEqual(
                    duplicate.structured_content,
                    {
                        "items": [
                            {
                                "id": "red-2020-2",
                                "possible_duplicate_of": "red-2020",
                            }
                        ]
                    },
                )

                typo = await client.call_tool(
                    "add_multiple_records",
                    {
                        "collection": "wines",
                        "records": [
                            {
                                "values": {"name": "Never Added"},
                                "profile_lnik": {},
                            }
                        ],
                    },
                )
                self.assertTrue(typo.is_error)
                self.assertIn("records[0]", typo.content[0].text)
                self.assertIn("profile_lnik", typo.content[0].text)

                full_batch = await client.call_tool(
                    "add_multiple_records",
                    {
                        "collection": "books",
                        "records": [{"values": {"title": "Piranesi", "year": 2020}}],
                        "minimal": False,
                    },
                )
                self.assertEqual(
                    full_batch.structured_content["items"][0]["record"]["id"],
                    "piranesi-2020",
                )
                self.assertIsNone(
                    full_batch.structured_content["items"][0]["link"]
                )

                batch_tool = next(
                    tool for tool in result.tools if tool.name == "add_multiple_records"
                )
                batch_description = batch_tool.description.lower()
                self.assertIn("use update_record", batch_description)
                self.assertIn("add_link", batch_description)
                self.assertIn("profile_link", batch_description)

                link_tool = next(tool for tool in result.tools if tool.name == "add_link")
                self.assertIn("from_id is 'self'", link_tool.description)

                migrate_tool = next(
                    tool for tool in result.tools if tool.name == "migrate"
                )
                migrate_description = migrate_tool.description.lower()
                self.assertIn("never use this for ordinary record", migrate_description)
                self.assertIn("add_multiple_records", migrate_description)

                migration = await client.call_tool(
                    "migrate",
                    {
                        "description": "verify compact result",
                        "statements": ["SELECT 1"],
                        "minimal": True,
                    },
                )
                self.assertEqual(
                    set(migration.structured_content),
                    {"migration_id", "snapshot", "warnings"},
                )

                restored = await client.call_tool(
                    "restore_snapshot",
                    {
                        "snapshot": migration.structured_content["snapshot"],
                        "minimal": True,
                    },
                )
                self.assertEqual(
                    set(restored.structured_content),
                    {"restored", "safety_snapshot"},
                )


if __name__ == "__main__":
    unittest.main()
