"""MCPClient concurrency contract.

The MCP stdio protocol is single-stream JSON-RPC. Two concurrent
``call_tool`` invocations on the same ``MCPClient`` must NOT race for stdout
lines; the lock added in :class:`MCPClient` serializes stdin writes + stdout
reads so each caller deterministically receives the response with its own
request id.
"""

from __future__ import annotations

import asyncio
import json
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
