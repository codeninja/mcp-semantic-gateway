import sys
import json
import time

def main():
    # Simple MCP server with 250 tools for stress testing
    tools = []
    for i in range(250):
        tools.append({
            "name": f"tool_{i}",
            "description": f"This is tool number {i}. It performs operation {i % 10} on category {i // 25}.",
            "inputSchema": {"type": "object", "properties": {"val": {"type": "number"}}}
        })

    for line in sys.stdin:
        try:
            req = json.loads(line)
            method = req.get("method")
            if method == "initialize":
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "StressDemo", "version": "1.0.0"}}}
            elif method == "tools/list":
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"tools": tools}}
            else:
                resp = {"jsonrpc": "2.0", "id": req["id"], "result": {}}
            
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except EOFError:
            break
        except Exception:
            break

if __name__ == "__main__":
    main()
