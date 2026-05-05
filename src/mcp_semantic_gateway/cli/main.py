import typer
from typing import Optional
from mcp_semantic_gateway import __version__
from mcp_semantic_gateway.cli.synth import synth_app
from mcp_semantic_gateway.cli.onboard import onboard as onboard_cmd
from mcp_semantic_gateway.cli.search import search_command
from mcp_semantic_gateway.cli.doctor import doctor_command

app = typer.Typer(name="mcp-semantic-gateway", help="Semantic Tool Discovery Middleware for MCP")
app.add_typer(synth_app, name="synth")
app.command("onboard")(onboard_cmd)
app.command("search")(search_command)
app.command("doctor")(doctor_command)

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
    """Initialize the gateway data directory and configuration."""
    from mcp_semantic_gateway.storage.init import initialize_data_dir
    base_dir = initialize_data_dir()
    typer.echo(f"Initialized mcp-semantic-gateway at {base_dir}")

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

if __name__ == "__main__":
    app()
