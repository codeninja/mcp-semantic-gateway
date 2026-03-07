import asyncio
import sys
import json
from toolsearch.config.loader import load_config
from toolsearch.storage.init import initialize_data_dir
from toolsearch.ingestion.collector import MCPClient
from toolsearch.ingestion.embedder import LocalEmbedder
from toolsearch.storage.vector_store import VectorStore
from toolsearch.storage.metadata_db import MetadataDB
import aiosqlite

class ToolSearchProxy:
    def __init__(self):
        self.config = load_config()
        self.base_dir = initialize_data_dir()
        self.clients: Dict[str, MCPClient] = {}
        self.query_context: Optional[str] = None
        self.embedder = LocalEmbedder(self.config.embedding.model_name)
        self.vector_store = VectorStore(self.base_dir / "index" / "vectors.db", dim=self.config.embedding.dimensions)
        self.vector_store.load()
        self.db_path = self.base_dir / "index" / "metadata.db"

    async def start(self):
        for server_id, server_config in self.config.servers.items():
            if server_config.enabled:
                client = MCPClient(server_id, server_config)
                await client.start()
                self.clients[server_id] = client

    async def run(self):
        await self.start()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        
        while True:
            line = await reader.readline()
            if not line:
                break
            
            try:
                req = json.loads(line)
                method = req.get("method")
                
                if method == "tools/list":
                    await self.handle_tools_list(req)
                elif method == "tools/call":
                    # Simple routing based on name
                    name = req["params"]["name"]
                    if name == "toolsearch_context":
                        self.query_context = req["params"]["arguments"]["query"]
                        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": f"Context set to: {self.query_context}"}]}}
                        sys.stdout.write(json.dumps(resp) + "\n")
                        sys.stdout.flush()
                    else:
                        # Forward to all for demo
                        # In real impl, use routing table
                        for client in self.clients.values():
                            client.process.stdin.write(line)
                            await client.process.stdin.drain()
                            # Forward response back (simplified)
                elif method == "initialize":
                    resp = {
                        "jsonrpc": "2.0", 
                        "id": req["id"], 
                        "result": {
                            "protocolVersion": "2024-11-05", 
                            "capabilities": {"tools": {}}, 
                            "serverInfo": {"name": "ToolSearchProxy", "version": "1.0.0"}
                        }
                    }
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
                else:
                    # Passthrough
                    pass
            except Exception as e:
                # Log error
                pass

    async def handle_tools_list(self, req):
        if not self.query_context:
            # Return all (simplified)
            # In real impl, harvest from all servers or use DB
            tools = []
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT name, description, input_schema FROM tools") as cursor:
                    async for row in cursor:
                        tools.append({
                            "name": row[0],
                            "description": row[1],
                            "inputSchema": json.loads(row[2]) if row[2] else {"type": "object"}
                        })
        else:
            # Semantic filtering
            query_vector = self.embedder.embed([self.query_context])[0]
            labels, scores = self.vector_store.knn_query(query_vector, k=self.config.retrieval.top_k)
            
            tools = []
            async with aiosqlite.connect(self.db_path) as db:
                for label in labels:
                    async with db.execute("SELECT name, description, input_schema FROM tools LIMIT 1 OFFSET ?", (label,)) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            tools.append({
                                "name": row[0],
                                "description": row[1],
                                "inputSchema": json.loads(row[2]) if row[2] else {"type": "object"}
                            })
        
        # Add toolsearch tools
        tools.append({
            "name": "toolsearch_context",
            "description": "Set the semantic context for tool discovery",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        })
        
        resp = {"jsonrpc": "2.0", "id": req.get("id"), "result": {"tools": tools}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    proxy = ToolSearchProxy()
    asyncio.run(proxy.run())
