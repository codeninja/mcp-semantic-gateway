import typer
from typing import Optional
from mcp_semantic_gateway import __version__
from mcp_semantic_gateway.cli.synth import synth_app
from mcp_semantic_gateway.cli.onboard import onboard as onboard_cmd

app = typer.Typer(name="mcp-semantic-gateway", help="Semantic Tool Discovery Middleware for MCP")
app.add_typer(synth_app, name="synth")
app.command("onboard")(onboard_cmd)

def version_callback(value: bool):
    if value:
        typer.echo(f"mcp-semantic-gateway version: {__version__}")
        raise typer.Exit()

@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None, "--version", callback=version_callback, is_eager=True, help="Show version and exit"
    ),
):
    pass

@app.command()
def init():
    """Initialize MCPSemanticGateway data directory and configuration."""
    from mcp_semantic_gateway.storage.init import initialize_data_dir
    initialize_data_dir()
    typer.echo("Initialized MCPSemanticGateway at ~/.mcp_semantic_gateway")

@app.command()
def index():
    """Rebuild or update the tool index from configured servers."""
    from mcp_semantic_gateway.ingestion.index_writer import run_indexing
    import asyncio
    asyncio.run(run_indexing())

@app.command()
def server():
    """Start the MCPSemanticGateway HTTP/SSE server."""
    from mcp_semantic_gateway.integration.server import start_server
    start_server()

@app.command()
def proxy():
    """Start the MCP proxy wrapper."""
    from mcp_semantic_gateway.integration.proxy import MCPSemanticGatewayProxy
    import asyncio
    proxy = MCPSemanticGatewayProxy()
    asyncio.run(proxy.run())

@app.command()
def search(query: str):
    """Search for tools semantically."""
    typer.echo(f"Searching for: {query}")

if __name__ == "__main__":
    app()
