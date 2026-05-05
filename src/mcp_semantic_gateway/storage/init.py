import tomlkit
from mcp_semantic_gateway.config.loader import gateway_home

def initialize_data_dir():
    base_dir = gateway_home()
    (base_dir / "index").mkdir(parents=True, exist_ok=True)
    (base_dir / "models").mkdir(parents=True, exist_ok=True)
    (base_dir / "logs").mkdir(parents=True, exist_ok=True)

    config_path = base_dir / "config.toml"
    if not config_path.exists():
        doc = tomlkit.document()
        doc.add(tomlkit.comment("MCPSemanticGateway Configuration"))
        
        retrieval = tomlkit.table()
        retrieval.add("top_k", 10)
        retrieval.add("min_score", 0.3)
        doc.add("retrieval", retrieval)
        
        embedding = tomlkit.table()
        embedding.add("backend", "local")
        embedding.add("model_name", "all-MiniLM-L6-v2")
        embedding.add("dimensions", 384)
        doc.add("embedding", embedding)
        
        proxy = tomlkit.table()
        proxy.add("mode", "filtered")
        proxy.add("context_ttl_seconds", 300)
        proxy.add("fallback_on_no_context", "all")
        doc.add("proxy", proxy)
        
        index = tomlkit.table()
        index.add("max_parallel_servers", 4)
        index.add("server_timeout_seconds", 30)
        doc.add("index", index)
        
        logging = tomlkit.table()
        logging.add("enabled", True)
        doc.add("logging", logging)
        
        # Empty servers table
        doc.add("servers", tomlkit.table())
        
        with open(config_path, "w") as f:
            f.write(tomlkit.dumps(doc))
    
    return base_dir
