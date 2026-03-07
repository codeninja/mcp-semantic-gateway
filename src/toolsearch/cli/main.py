import typer
from typing import Optional
from toolsearch import __version__

app = typer.Typer(name="tool-search", help="Semantic Tool Discovery Middleware for MCP")

def version_callback(value: bool):
    if value:
        typer.echo(f"tool-search version: {__version__}")
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
    """Initialize ToolSearch data directory and configuration."""
    from toolsearch.storage.init import initialize_data_dir
    initialize_data_dir()
    typer.echo("Initialized ToolSearch at ~/.toolsearch")

@app.command()
def index():
    """Rebuild or update the tool index from configured servers."""
    from toolsearch.ingestion.index_writer import run_indexing
    import asyncio
    asyncio.run(run_indexing())

@app.command()
def server():
    """Start the ToolSearch HTTP/SSE server."""
    from toolsearch.integration.server import start_server
    start_server()

@app.command()
def proxy():
    """Start the MCP proxy wrapper."""
    from toolsearch.integration.proxy import ToolSearchProxy
    import asyncio
    proxy = ToolSearchProxy()
    asyncio.run(proxy.run())

@app.command()
def search(query: str):
    """Search for tools semantically."""
    typer.echo(f"Searching for: {query}")

if __name__ == "__main__":
    app()
