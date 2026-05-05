"""Interactive chat CLI: a model with native MCP access to the petstore.

Architecture::

    chat.py ─▶ openai-agents Runner ─▶ Agent
                    │
                    │ mcp_servers=[ MCPServerStdio(...) ]
                    ▼
            mcp-semantic-gateway proxy   (real stdio MCP server)
                    │  (every request/response also tee'd to mcp-events.log)
                    ▼
            OpenAPIExecutor ─▶ petstore HTTP backend (FastAPI)

Persistent state lives next to this script in ``gateway_state/``:

* ``config.toml`` -- regenerated each run with the petstore's ephemeral URL.
* ``index/metadata.db`` + ``index/vectors.db`` -- the gateway's indexed tool
  catalog. Port-independent (FastAPI emits no ``servers`` block) so the
  binaries are safe to commit and skip re-indexing on subsequent runs.
* ``mcp-events.log`` -- raw JSON-RPC traffic between the Agents SDK and the
  gateway proxy. The chat tails this file and prints events live.

Multi-turn: history is preserved across turns within a session. Each
session is otherwise fresh.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

import click
import httpx
import tomlkit
import uvicorn
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_HERE))

from agents import (  # noqa: E402
    Agent,
    OpenAIChatCompletionsModel,
    Runner,
    set_default_openai_api,
    set_tracing_disabled,
)
from agents.mcp import MCPServerStdio  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from mcp_semantic_gateway.config.models import (  # noqa: E402
    LLMConfig,
    MCPSemanticGatewayConfig,
    ServerConfig,
    SkillGenerationConfig,
    SourceType,
)
from mcp_semantic_gateway.ingestion.index_writer import index_all  # noqa: E402
from mcp_semantic_gateway.storage.metadata_db import MetadataDB  # noqa: E402

from petstore_backend import app as petstore_app  # noqa: E402


GATEWAY_STATE = _HERE / "gateway_state"
CONFIG_TOML = GATEWAY_STATE / "config.toml"
INDEX_DB = GATEWAY_STATE / "index" / "metadata.db"
RPC_LOG = GATEWAY_STATE / "mcp-events.log"
SKILLS_OUTPUT_DIR = "synth"  # gateway_state/synth/skills/...
SKILLS_PATH = GATEWAY_STATE / SKILLS_OUTPUT_DIR / "skills"


# ---------------------------------------------------------------------------
# Petstore backend supervisor
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _running_petstore() -> Iterator[str]:
    port = _free_port()
    cfg = uvicorn.Config(
        petstore_app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/openapi.json", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise RuntimeError("petstore failed to start")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Gateway state preparation
# ---------------------------------------------------------------------------


def _build_runtime_config(
    backend_url: str,
    *,
    with_skill_gen: bool,
    with_skills_source: bool,
    model: str,
    base_url: Optional[str],
    api_key_env: str,
) -> MCPSemanticGatewayConfig:
    """Construct the in-memory gateway config used for indexing + synthesis.

    ``config.toml`` is written from this same shape via
    :func:`_write_config_from`, so what's on disk and what's passed to
    ``index_all`` always agree.
    """

    servers: dict[str, ServerConfig] = {
        "petstore": ServerConfig(
            type=SourceType.OPENAPI,
            url=f"{backend_url}/openapi.json",
            base_url=backend_url,
            enabled=True,
            generate_skills=with_skill_gen,
        ),
    }
    if with_skills_source:
        servers["petstore-skills"] = ServerConfig(
            type=SourceType.SKILL,
            path=str(SKILLS_PATH),
            enabled=True,
        )

    llm: Optional[LLMConfig] = None
    if with_skill_gen:
        llm = LLMConfig(
            provider="openai-compatible",
            model=model,
            base_url=base_url or "https://api.openai.com/v1",
            api_key_env=api_key_env,
        )

    return MCPSemanticGatewayConfig(
        servers=servers,
        llm=llm,
        skill_generation=SkillGenerationConfig(
            enabled=with_skill_gen,
            output_dir=SKILLS_OUTPUT_DIR,
        ),
    )


def _write_config_from(home_dir: Path, cfg: MCPSemanticGatewayConfig) -> None:
    """Mirror the runtime config to ``config.toml`` so the gateway proxy
    subprocess (which reads from disk) sees the same shape."""

    home_dir.mkdir(parents=True, exist_ok=True)
    cfg_doc = tomlkit.document()
    cfg_doc.add(tomlkit.comment("Auto-generated by examples/petstore_chat/chat.py."))
    cfg_doc.add(tomlkit.comment("URL embeds an ephemeral port, so this file is gitignored."))

    servers_tbl = tomlkit.table()
    for sid, sc in cfg.servers.items():
        entry = tomlkit.table()
        entry["type"] = sc.type.value
        if sc.url:
            entry["url"] = sc.url
        if sc.base_url:
            entry["base_url"] = sc.base_url
        if sc.path:
            # Persist Skill paths as relative to ``gateway_state/`` (the
            # config.toml directory) so the on-disk artifact is portable.
            # ``load_config`` resolves these against the config dir.
            p = Path(sc.path)
            try:
                rel = p.resolve().relative_to(home_dir.resolve())
                entry["path"] = str(rel)
            except ValueError:
                entry["path"] = str(p)
        entry["enabled"] = sc.enabled
        if sc.generate_skills:
            entry["generate_skills"] = True
        servers_tbl[sid] = entry
    cfg_doc["servers"] = servers_tbl

    if cfg.llm is not None:
        llm_tbl = tomlkit.table()
        llm_tbl["provider"] = cfg.llm.provider
        llm_tbl["model"] = cfg.llm.model
        llm_tbl["base_url"] = cfg.llm.base_url
        llm_tbl["api_key_env"] = cfg.llm.api_key_env
        cfg_doc["llm"] = llm_tbl

    if cfg.skill_generation.enabled:
        sg_tbl = tomlkit.table()
        sg_tbl["enabled"] = True
        sg_tbl["output_dir"] = cfg.skill_generation.output_dir
        cfg_doc["skill_generation"] = sg_tbl

    CONFIG_TOML.write_text(tomlkit.dumps(cfg_doc))


async def _index_and_summarize(
    home_dir: Path, cfg: MCPSemanticGatewayConfig, *, force: bool, console: Console,
) -> tuple[list[dict[str, Any]], bool]:
    """Run ``index_all`` (rebuilding if ``force`` or no metadata.db) and
    return (catalog rows, was_built)."""

    if force:
        shutil.rmtree(home_dir / "index", ignore_errors=True)

    built = False
    if not INDEX_DB.exists():
        console.print("[dim]indexing…[/]")
        await index_all(cfg, home_dir, log=lambda *_: None)
        built = True

    db = MetadataDB(INDEX_DB)
    rows = await db.list_tool_summaries()
    catalog = [{"name": r[2], "server": r[1], "type": r[3]} for r in rows]
    return catalog, built


# ---------------------------------------------------------------------------
# RPC event tail
# ---------------------------------------------------------------------------


_METHOD_COLOR = {
    "initialize": "yellow",
    "tools/list": "green",
    "tools/call": "magenta",
    "prompts/list": "green",
}


def _format_rpc_line(line: str, console: Console) -> Optional[Text]:
    try:
        ts_str, direction, payload = line.rstrip("\n").split("\t", 2)
    except ValueError:
        return None
    try:
        msg = json.loads(payload)
    except (ValueError, TypeError):
        return None
    ts = float(ts_str)
    when = time.strftime("%H:%M:%S", time.localtime(ts))

    if direction == "in":
        method = msg.get("method", "?")
        rid = msg.get("id", "-")
        color = _METHOD_COLOR.get(method, "cyan")
        params = msg.get("params") or {}
        if method == "tools/call":
            name = params.get("name", "?")
            args = params.get("arguments", {})
            summary = f"{name}({json.dumps(args, ensure_ascii=False)})"
        else:
            summary = json.dumps(params, ensure_ascii=False)[:120]
        text = Text()
        text.append(f"[{when}] ", style="dim")
        text.append("→ MCP ", style="dim")
        text.append(f"{method}", style=f"bold {color}")
        text.append(f" #{rid}", style="dim")
        if summary and summary != "{}":
            text.append(f"  {summary}", style="dim")
        return text

    # direction == "out"
    rid = msg.get("id", "-")
    if "error" in msg:
        err = msg["error"]
        text = Text()
        text.append(f"[{when}] ", style="dim")
        text.append("← MCP ", style="dim")
        text.append("error", style="bold red")
        text.append(f" #{rid}: {err.get('message', err)}", style="red")
        return text

    result = msg.get("result", {})
    summary = ""
    if isinstance(result, dict):
        if "tools" in result:
            summary = f"{len(result['tools'])} tools"
        elif "content" in result:
            err = result.get("isError")
            blocks = result.get("content", [])
            if err:
                summary = f"isError; {len(blocks)} content block(s)"
            else:
                summary = f"{len(blocks)} content block(s)"
                if "structuredContent" in result:
                    summary += " + structured"
        elif "protocolVersion" in result:
            summary = f"proto {result['protocolVersion']}"
    text = Text()
    text.append(f"[{when}] ", style="dim")
    text.append("← MCP ", style="dim")
    text.append("result", style="bold green")
    text.append(f" #{rid}", style="dim")
    if summary:
        text.append(f"  {summary}", style="dim")
    return text


async def _tail_rpc_log(
    path: Path, stop: asyncio.Event, console: Console, *, verbose: bool
) -> None:
    """Tail the proxy's RPC log file and print formatted events."""

    last_pos = path.stat().st_size if path.exists() else 0
    while not stop.is_set():
        try:
            await asyncio.sleep(0.1)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size <= last_pos:
                continue
            with path.open("r") as f:
                f.seek(last_pos)
                chunk = f.read()
                last_pos = f.tell()
            for line in chunk.splitlines():
                rendered = _format_rpc_line(line, console)
                if rendered:
                    console.print(rendered)
                if verbose:
                    parts = line.split("\t", 2)
                    if len(parts) == 3:
                        try:
                            obj = json.loads(parts[2])
                            console.print(
                                Panel(
                                    Text(json.dumps(obj, indent=2), style="dim"),
                                    border_style="dim",
                                    expand=False,
                                )
                            )
                        except Exception:
                            pass
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            # Tail is best-effort -- never crash the chat over log issues.
            await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# Skill synthesis
# ---------------------------------------------------------------------------


async def _run_synthesis(
    home_dir: Path,
    cfg: MCPSemanticGatewayConfig,
    *,
    console: Console,
) -> list[dict[str, Any]]:
    """Run the gateway's synth pipeline (mine -> cluster -> SKILL.md).

    Returns metadata about each skill written.
    """

    # ``_run_pipeline`` calls ``initialize_data_dir()`` internally, which
    # reads ``MCP_SEMANTIC_GATEWAY_HOME``. Make sure both the synth pass and
    # the later proxy subprocess agree on the same home.
    os.environ["MCP_SEMANTIC_GATEWAY_HOME"] = str(home_dir)

    # Imported here to avoid pulling typer + LLM deps at module load when
    # the user isn't running synthesis.
    from mcp_semantic_gateway.cli.synth import _run_pipeline

    console.print(
        f"[dim]running skill synthesis (mine → cluster → SKILL.md) "
        f"with {cfg.llm.model if cfg.llm else '?'}…[/]"
    )
    code = await _run_pipeline(
        cfg,
        cfg.servers,
        dry_run=False,
        progress=False,  # plain output; rich progress would clash with our panel
        skills_only=False,
        skip_skills=False,
        project_root=home_dir,
    )
    if code != 0:
        raise RuntimeError(f"synth pipeline returned exit code {code}")

    # Walk the skills tree and pull each ``.meta.json`` for a tidy summary.
    skills: list[dict[str, Any]] = []
    if SKILLS_PATH.exists():
        for meta_path in sorted(SKILLS_PATH.rglob(".meta.json")):
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            skill_md = meta_path.parent / "SKILL.md"
            skills.append(
                {
                    "skill_id": meta.get("skill_id", "?"),
                    "server_id": meta.get("source", {}).get("server_id", "?"),
                    "tool_dependencies": meta.get("tool_dependencies", []),
                    "skill_md": skill_md,
                }
            )
    return skills


# ---------------------------------------------------------------------------
# Model wiring
# ---------------------------------------------------------------------------


def _build_model(model: str, base_url: Optional[str], api_key: str):
    if base_url is None:
        return model
    set_tracing_disabled(True)
    set_default_openai_api("chat_completions")
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    return OpenAIChatCompletionsModel(model=model, openai_client=client)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


SYSTEM_INSTRUCTIONS = (
    "You are a helpful assistant with MCP tools to query and modify a "
    "petstore (pets, orders, users). Be concise.\n"
    "\n"
    "Skill discovery — IMPORTANT:\n"
    "Before any task that touches 2+ resources or strings together "
    "multiple endpoints, follow this two-step flow:\n"
    "  1. Call mcp_semantic_gateway_find_skills with a query describing "
    "     the task. The result is a list of skill names + descriptions.\n"
    "  2. If a relevant skill appears, call mcp_semantic_gateway_get_skill "
    "     with that skill's `name` to retrieve the full procedure. "
    "     Follow the procedure's steps using the tools it lists.\n"
    "\n"
    "For trivial single-tool lookups (e.g. 'get pet 3', 'show inventory'), "
    "skip skill discovery and just call the tool directly."
)


GREETING = (
    "I can assist you with tasks related to a pet store, such as:\n"
    "\n"
    "1. Managing pets (adding, updating, deleting).\n"
    "2. Searching for pets by status or tags.\n"
    "3. Placing and managing orders.\n"
    "4. Creating and managing user accounts.\n"
    "\n"
    "If you have a specific task in mind, just let me know!"
)


def _print_greeting(console: Console) -> None:
    console.print(Markdown(GREETING))
    console.print()


def _seed_history() -> list[Any]:
    return [{"role": "assistant", "content": GREETING}]


async def _chat_loop(agent: Agent, console: Console) -> None:
    history: list[Any] = _seed_history()
    console.print(
        "\n[dim]Multi-turn chat. /quit, /exit, or Ctrl-D to leave. "
        "/clear to reset history.[/]\n"
    )
    _print_greeting(console)
    while True:
        try:
            user_input = console.input("[bold cyan]you ▸[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not user_input:
            continue
        if user_input in {"/quit", "/exit", "/q"}:
            return
        if user_input == "/clear":
            history = _seed_history()
            console.print("[dim]history cleared[/]")
            _print_greeting(console)
            continue

        history.append({"role": "user", "content": user_input})
        try:
            result = await Runner.run(agent, history)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]agent error:[/] {e}")
            continue
        history = result.to_input_list()
        if result.final_output:
            console.print(Markdown(str(result.final_output)))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.argument("query", required=False)
@click.option("--model", default="gpt-4o-mini", show_default=True,
              help="LLM model id.")
@click.option("--base-url", default=None,
              help="OpenAI-compatible endpoint URL (e.g. http://localhost:11434/v1 for Ollama). "
                   "Omit to use real OpenAI.")
@click.option("--api-key-env", default="OPENAI_API_KEY", show_default=True,
              help="Environment variable holding the API key.")
@click.option("--rebuild-index", is_flag=True,
              help="Discard the persistent index and rebuild from the spec.")
@click.option("--verbose-rpc", is_flag=True,
              help="Print full JSON for every MCP request/response, not just a one-liner.")
@click.option("--generate-skills", is_flag=True,
              help="Run the gateway's synthesis pipeline (mine → cluster → SKILL.md) "
                   "with the chat's LLM, then add the generated skills as a Skill source "
                   "and re-index so the agent can discover them via find_skills.")
def main(
    query: Optional[str],
    model: str,
    base_url: Optional[str],
    api_key_env: str,
    rebuild_index: bool,
    verbose_rpc: bool,
    generate_skills: bool,
) -> None:
    """Chat with an LLM agent that has MCP access to the petstore.

    With no QUERY: enter an interactive REPL.
    With QUERY: single-shot mode, ask once and exit.
    """

    api_key = os.environ.get(api_key_env, "not-needed")
    console = Console()

    async def _run() -> None:
        with _running_petstore() as backend_url:
            # 1. Tools-only index (always).
            tool_cfg = _build_runtime_config(
                backend_url,
                with_skill_gen=False,
                with_skills_source=False,
                model=model, base_url=base_url, api_key_env=api_key_env,
            )
            _write_config_from(GATEWAY_STATE, tool_cfg)
            catalog, tool_built = await _index_and_summarize(
                GATEWAY_STATE, tool_cfg, force=rebuild_index, console=console,
            )

            # 2. Optional skill synthesis. The pipeline reads use cases from
            # the same metadata.db we just built, runs an LLM to mine them,
            # clusters, and writes SKILL.md packages under
            # ``gateway_state/synth/skills/...``.
            generated_skills: list[dict[str, Any]] = []
            if generate_skills:
                synth_cfg = _build_runtime_config(
                    backend_url,
                    with_skill_gen=True,
                    with_skills_source=False,  # skills don't exist yet
                    model=model, base_url=base_url, api_key_env=api_key_env,
                )
                _write_config_from(GATEWAY_STATE, synth_cfg)
                generated_skills = await _run_synthesis(
                    GATEWAY_STATE, synth_cfg, console=console,
                )

                # 3. Re-index with the skills source so the gateway picks
                # them up alongside the openapi tools.
                full_cfg = _build_runtime_config(
                    backend_url,
                    with_skill_gen=True,
                    with_skills_source=True,
                    model=model, base_url=base_url, api_key_env=api_key_env,
                )
                _write_config_from(GATEWAY_STATE, full_cfg)
                catalog, _ = await _index_and_summarize(
                    GATEWAY_STATE, full_cfg, force=True, console=console,
                )
                tool_built = True

            # 4. Status panel: show the user exactly what was created.
            tools = [c for c in catalog if c["type"] == "tool"]
            skills = [c for c in catalog if c["type"] == "skill"]

            status = Text()
            status.append("petstore backend  ", style="bold")
            status.append(f"{backend_url}\n")
            status.append("gateway state     ", style="bold")
            status.append(f"{GATEWAY_STATE}\n")
            status.append("config            ", style="bold")
            status.append(f"{CONFIG_TOML.relative_to(_REPO)} (regenerated)\n")
            status.append("index             ", style="bold")
            verb = "freshly built" if tool_built else "reused from disk"
            status.append(f"{INDEX_DB.relative_to(_REPO)}  [{verb}]\n")
            status.append("rpc event log     ", style="bold")
            status.append(f"{RPC_LOG.relative_to(_REPO)}\n")
            status.append("indexed tools     ", style="bold")
            tool_names = ", ".join(t["name"] for t in tools) or "(none)"
            status.append(f"{len(tools)}: {tool_names}\n")
            status.append("indexed skills    ", style="bold")
            if skills:
                skill_names = ", ".join(s["name"] for s in skills)
                status.append(f"{len(skills)}: {skill_names}")
            else:
                status.append("0 (run with --generate-skills to synthesize)")
            console.print(Panel(status, title="petstore-chat", expand=False))

            # If we synthesized this run, surface the generated SKILL.md
            # packages with their tool dependencies so the user can see
            # what the LLM produced.
            if generated_skills:
                skill_lines = Text()
                for s in generated_skills:
                    skill_lines.append(f"  • {s['skill_id']}\n", style="bold")
                    deps = ", ".join(s["tool_dependencies"][:6]) or "(none)"
                    skill_lines.append(f"    tools: {deps}\n", style="dim")
                    try:
                        rel = s["skill_md"].relative_to(_REPO)
                    except ValueError:
                        rel = s["skill_md"]
                    skill_lines.append(f"    file:  {rel}\n", style="dim")
                console.print(
                    Panel(skill_lines, title="generated skills", expand=False)
                )

            # Reset the RPC log for this session.
            RPC_LOG.write_text("")
            stop = asyncio.Event()
            tail = asyncio.create_task(
                _tail_rpc_log(RPC_LOG, stop, console, verbose=verbose_rpc)
            )

            try:
                async with MCPServerStdio(
                    name="petstore-gateway",
                    params={
                        "command": sys.executable,
                        "args": ["-m", "mcp_semantic_gateway.cli.main", "proxy"],
                        "env": {
                            **os.environ,
                            "MCP_SEMANTIC_GATEWAY_HOME": str(GATEWAY_STATE),
                            "MCP_SEMANTIC_GATEWAY_LOG_RPC": str(RPC_LOG),
                        },
                    },
                ) as gateway:
                    agent = Agent(
                        name="PetstoreAgent",
                        instructions=SYSTEM_INSTRUCTIONS,
                        mcp_servers=[gateway],
                        model=_build_model(model, base_url, api_key),
                    )
                    if query:
                        result = await Runner.run(agent, query)
                        if result.final_output:
                            console.print(Markdown(str(result.final_output)))
                    else:
                        await _chat_loop(agent, console)
            finally:
                stop.set()
                tail.cancel()
                try:
                    await tail
                except (asyncio.CancelledError, Exception):
                    pass

    asyncio.run(_run())


if __name__ == "__main__":
    main()
