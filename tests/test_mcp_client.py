"""MCPClient concurrency + transport contracts.

* Concurrency: the MCP stdio protocol is single-stream JSON-RPC. Two
  concurrent ``call_tool`` invocations on the same ``MCPClient`` must NOT
  race for stdout lines; the lock serializes stdin writes + stdout reads so
  each caller deterministically receives the response with its own request
  id.
* Buffer limit: a single ``tools/list`` response can exceed 64 KiB on
  large upstream catalogs. The default asyncio StreamReader limit must be
  raised so the read does not raise ``LimitOverrunError``.
* Transport close: ``stop()`` must close the subprocess transport and
  drop the reference so its finalizer does not run on a closed event loop
  at interpreter shutdown.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import List

import pytest

from mcp_semantic_gateway.config.models import ServerConfig, SourceType
from mcp_semantic_gateway.ingestion.collector import MCPClient


class _FakeStdin:
    def __init__(self, sink: List[bytes]) -> None:
        self._sink = sink

    def write(self, data: bytes) -> None:
        self._sink.append(data)

    async def drain(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, sink: List[bytes], stdout: asyncio.StreamReader) -> None:
        self.stdin = _FakeStdin(sink)
        self.stdout = stdout


@pytest.mark.asyncio
async def test_call_tool_serializes_concurrent_invocations():
    client = MCPClient("svc", ServerConfig(type=SourceType.MCP, command="dummy"))

    written: List[bytes] = []
    stdout = asyncio.StreamReader()
    client.process = _FakeProcess(written, stdout)

    # Kick off both calls. Without the lock, both would write to stdin and
    # both readers would race for the next stdout line.
    t1 = asyncio.create_task(client.call_tool("a", {}, request_id=101))
    t2 = asyncio.create_task(client.call_tool("b", {}, request_id=202))

    # Yield enough event-loop turns for whichever call won the lock to have
    # written its request and parked on ``stdout.readline()``.
    for _ in range(10):
        await asyncio.sleep(0)

    # Lock contract: only one request is on the wire so far.
    assert len(written) == 1, [json.loads(w) for w in written]
    first_req = json.loads(written[0])
    assert first_req["id"] == 101  # tasks acquire in declaration order

    # Feed the response for id=101. t1 completes, releases the lock, t2 wakes.
    stdout.feed_data(
        (json.dumps({"jsonrpc": "2.0", "id": 101, "result": {"x": 101}}) + "\n").encode()
    )
    for _ in range(10):
        await asyncio.sleep(0)

    # Now the second request must be on the wire.
    assert len(written) == 2
    assert json.loads(written[1])["id"] == 202

    stdout.feed_data(
        (json.dumps({"jsonrpc": "2.0", "id": 202, "result": {"x": 202}}) + "\n").encode()
    )

    res_a = await asyncio.wait_for(t1, timeout=1.0)
    res_b = await asyncio.wait_for(t2, timeout=1.0)
    assert res_a["result"] == {"x": 101}
    assert res_b["result"] == {"x": 202}


# ---------------------------------------------------------------------------
# Buffer-limit regression: a real upstream MCP child must be able to emit a
# tools/list response larger than asyncio's 64 KiB default StreamReader
# limit without raising LimitOverrunError on readline().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tools_list_handles_response_larger_than_default_limit(tmp_path):
    """Spawn a real subprocess that emits a >256 KiB JSON-RPC line and
    confirm MCPClient reads it. With asyncio's default 64 KiB StreamReader
    limit this raises LimitOverrunError. The fix passes ``limit=`` to
    ``create_subprocess_exec``.
    """

    # Generate ~300 tools whose stringified JSON-RPC line is well over 64 KiB.
    big_tools = [
        {
            "name": f"tool_{i:04d}",
            "description": "x" * 800,
            "inputSchema": {"type": "object", "properties": {}},
        }
        for i in range(300)
    ]
    init_resp = {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}
    list_resp = {"jsonrpc": "2.0", "id": 2, "result": {"tools": big_tools}}

    encoded_list_line = (json.dumps(list_resp) + "\n").encode()
    assert len(encoded_list_line) > 256 * 1024, (
        f"fixture too small ({len(encoded_list_line)} bytes); "
        "test would not exercise the >64 KiB path"
    )

    # Tiny stdio script: read two newline-terminated requests, reply with
    # init_resp then list_resp. Pure stdlib so it runs in any test env.
    script = tmp_path / "fake_mcp.py"
    script.write_text(
        "import sys, json\n"
        "sys.stdin.readline()\n"
        f"sys.stdout.write({json.dumps(json.dumps(init_resp))} + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n"
        f"sys.stdout.write({json.dumps(json.dumps(list_resp))} + '\\n')\n"
        "sys.stdout.flush()\n"
    )

    cfg = ServerConfig(type=SourceType.MCP, command=sys.executable, args=[str(script)])
    client = MCPClient("big-catalog", cfg)
    await client.start()
    try:
        tools = await asyncio.wait_for(client.call_tools_list(), timeout=10.0)
    finally:
        await client.stop()

    assert len(tools) == 300
    assert tools[0]["name"] == "tool_0000"
    assert tools[-1]["name"] == "tool_0299"


# ---------------------------------------------------------------------------
# Transport-cleanup regression: stop() must close the subprocess transport
# and clear ``self.process`` so the finalizer does not later raise
# "RuntimeError: Event loop is closed" at interpreter shutdown.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_closes_transport_and_clears_process(tmp_path):
    script = tmp_path / "noop.py"
    script.write_text("import sys; sys.stdin.read()\n")

    cfg = ServerConfig(type=SourceType.MCP, command=sys.executable, args=[str(script)])
    client = MCPClient("noop", cfg)
    await client.start()
    transport = client.process._transport
    assert not transport.is_closing()

    await client.stop()

    assert client.process is None, "stop() must drop the process reference"
    assert transport.is_closing(), "stop() must close the subprocess transport"

    # Second stop() must be a no-op, not raise.
    await client.stop()
