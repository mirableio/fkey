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


if __name__ == "__main__":
    unittest.main()
