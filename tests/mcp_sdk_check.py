"""Optional interoperability check with the official MCP Python SDK 2.x.

Not imported by the dependency-free unit suite. Run with the test-only SDK venv.
"""

import asyncio
from importlib.metadata import version
from pathlib import Path
import sys
import tempfile

from context_kernel.common import canonical
from context_kernel.store import Store


async def check():
    from jsonschema import Draft202012Validator
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="context-kernel-sdk-") as directory:
        path = Path(directory) / "memory.sqlite"
        store = Store(path, create=True)
        private = Store(path, scope="private")
        try:
            public = store.remember("user", "salary", 42000, "Annual salary is 42000.")
            private.remember("user", "salary", "PRIVATE_CANARY", "PRIVATE_CANARY")
            params = StdioServerParameters(command=sys.executable,
                args=["-m", "context_kernel", "--db", str(path), "--scope", "personal", "serve"], cwd=root)
            async with Client(params, read_timeout_seconds=10) as client:
                tools = await client.list_tools()
                assert len(tools.tools) == 16
                for tool in tools.tools:
                    Draft202012Validator.check_schema(tool.input_schema)
                status = await client.call_tool("memory_status", {})
                assert status.structured_content["current_statements"] == 1
                context = await client.call_tool("memory_context", {"query": "My salary"})
                assert public["id"] in canonical(context.structured_content)
                assert "PRIVATE_CANARY" not in canonical(context.structured_content)
                proposal = await client.call_tool("memory_propose", {"operation": "correct", "payload": {
                    "target_id": public["id"], "value": 52000, "evidence": "My salary changed to 52000."}})
                assert store.records()[0]["value"] == 42000
                store.approve(proposal.structured_content["id"])
                fresh = await client.call_tool("memory_context", {"query": "My salary"})
                assert fresh.structured_content["context"]["claims"][0]["value"] == 52000
                why = await client.call_tool("memory_why", {"id": fresh.structured_content["projection_id"]})
                assert why.structured_content["host_attachment"] == "unknown"
                missing = await client.call_tool("memory_inspect", {"id": "missing"})
                assert missing.is_error
                inventory = await client.call_tool("memory_inventory", {})
                assert "PRIVATE_CANARY" not in canonical(inventory.structured_content)
                assert inventory.structured_content["entities"]["user"][0]["value"] == 52000
                # No hook ever bound a session to this server: writes and deletions are refused.
                capture = await client.call_tool("memory_capture", {"token": "forged-token-123",
                    "facts": [{"entity": "user", "predicate": "manager", "value": "Mallory"}]})
                forget = await client.call_tool("memory_forget", {"token": "forged-token-123", "id": public["id"]})
                assert capture.is_error and forget.is_error
                assert len(store.records()) == 1
                output = {"passed": True, "sdk_version": version("mcp"), "negotiated_protocol": client.protocol_version,
                          "tools": [t.name for t in tools.tools], "paid_api_calls": 0}
            return output
        finally:
            private.close()
            store.close()


if __name__ == "__main__":
    print(canonical(asyncio.run(check())))
