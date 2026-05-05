"""``mcp-semantic-gateway search`` command implementation.

Prints what the gateway would return for a given query against the local
vector index. Used as a sanity check after configuring sources and
running ``mcp-semantic-gateway index``.
"""

from __future__ import annotations

import asyncio
import json as _json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from mcp_semantic_gateway.config.loader import load_config
from mcp_semantic_gateway.retrieval.core import SearchCore
from mcp_semantic_gateway.retrieval.registry import ToolRegistry
from mcp_semantic_gateway.storage.init import initialize_data_dir


async def _run(
    query: str,
    top_k: int,
    item_type: Optional[str],
    as_json: bool,
) -> int:
    base_dir: Path = initialize_data_dir()
    config = load_config()
    metadata_db = base_dir / "index" / "metadata.db"
    vector_db = base_dir / "index" / "vectors.db"

    console = Console()

    if not metadata_db.exists() or not vector_db.exists():
        console.print(
            "[red]✗[/red] No index found at "
            f"[cyan]{base_dir / 'index'}[/cyan]. "
            "Run [bold]mcp-semantic-gateway index[/bold] first."
        )
        return 2

    registry = ToolRegistry(
        metadata_db, force_namespace=config.proxy.namespace_collisions
    )
    await registry.initialize()
    core = SearchCore(config, base_dir, registry=registry)

    item_types = [item_type] if item_type else None
    matches = await core.search_with_scores(
        query, top_k=top_k, item_types=item_types
    )

    if as_json:
        typer.echo(_json.dumps(matches, indent=2))
        return 0 if matches else 1

    if not matches:
        console.print(
            f"[yellow]No matches for[/yellow] [bold]{query!r}[/bold]. "
            "Index may be empty or query too narrow — try a broader phrase, "
            "or re-run [bold]mcp-semantic-gateway index[/bold]."
        )
        return 1

    table = Table(
        title=f"Top {len(matches)} matches for: {query!r}",
        show_lines=False,
        title_style="bold",
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Score", justify="right", style="green")
    table.add_column("Type", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Source", style="magenta")
    table.add_column("Description", overflow="fold")

    for rank, m in enumerate(matches, start=1):
        desc = m["description"] or ""
        if len(desc) > 80:
            desc = desc[:77] + "..."
        table.add_row(
            str(rank),
            f"{m['score']:.3f}",
            m["item_type"],
            m["name"],
            m["server_id"],
            desc,
        )

    console.print(table)
    return 0


def search_command(
    query: str = typer.Argument(..., help="Search query (free text)."),
    top_k: int = typer.Option(
        10, "--top-k", "-k", min=1, max=100, help="Maximum results to return."
    ),
    item_type: Optional[str] = typer.Option(
        None,
        "--type",
        "-t",
        help="Restrict to a single item type (tool, skill, or prompt).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Sanity-check what the gateway would return for a given query.

    Runs the same retrieval path the proxy uses for ``tools/list`` after a
    context has been set, but with explicit query text. Returns matches
    with name, source ID, item type, score, and description.
    """

    exit_code = asyncio.run(_run(query, top_k, item_type, as_json))
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
