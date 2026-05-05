"""CLI command for onboarding coding agents to the gateway's bundled skills.

Implements ``mcp-semantic-gateway onboard <provider>`` which copies the
shipped ``SKILL.md`` packages (consumer + development) into the directory
the named provider already reads at startup.

Provider -> user-level skill directory:

* ``claude``    -> ``~/.claude/skills/``
* ``codex``     -> ``~/.agents/skills/``  (per developers.openai.com/codex/skills)
* ``opencode``  -> ``~/.config/opencode/skills/``
* ``pi``        -> ``~/.pi/agent/skills/``

The ``--project`` flag installs into the cwd-relative equivalents instead.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Iterable

import typer
from rich.console import Console
from rich.table import Table


@dataclass(frozen=True)
class ProviderTarget:
    """Where a given provider expects to find skills on disk."""

    name: str
    user_dir: Path
    project_dir: Path
    notes: str

    def resolve(self, *, project: bool) -> Path:
        return self.project_dir if project else self.user_dir


_HOME = Path.home()


PROVIDERS: dict[str, ProviderTarget] = {
    "claude": ProviderTarget(
        name="claude",
        user_dir=_HOME / ".claude" / "skills",
        project_dir=Path(".claude") / "skills",
        notes="Claude Code / Claude Desktop. Both user and project paths are auto-discovered.",
    ),
    "codex": ProviderTarget(
        name="codex",
        user_dir=_HOME / ".agents" / "skills",
        project_dir=Path(".agents") / "skills",
        notes=(
            "OpenAI Codex CLI / IDE. Codex scans `.agents/skills` from cwd up "
            "to the repo root, plus `~/.agents/skills` for user-level skills."
        ),
    ),
    "opencode": ProviderTarget(
        name="opencode",
        user_dir=_HOME / ".config" / "opencode" / "skills",
        project_dir=Path(".opencode") / "skills",
        notes=(
            "opencode's native skills location. opencode also auto-discovers "
            "`~/.claude/skills` and `~/.agents/skills`, so you may already be "
            "covered if you have onboarded another agent."
        ),
    ),
    "pi": ProviderTarget(
        name="pi",
        user_dir=_HOME / ".pi" / "agent" / "skills",
        project_dir=Path(".pi") / "skills",
        notes=(
            "pi coding agent. pi also auto-discovers `~/.agents/skills` for "
            "cross-agent portability."
        ),
    ),
}


COLLECTIONS = ("consumer", "development", "all")


@dataclass(frozen=True)
class _BundledSkill:
    collection: str  # "consumer" or "development"
    name: str        # skill directory name
    source: Traversable  # importlib.resources handle to the dir


def iter_bundled_skills(collection: str = "all") -> Iterable[_BundledSkill]:
    """Yield every skill bundled in the wheel under the requested collection.

    ``collection="all"`` yields both consumer and development skills.
    """

    if collection not in COLLECTIONS:
        raise ValueError(
            f"unknown collection {collection!r}; expected one of {COLLECTIONS}"
        )

    targets: tuple[str, ...] = (
        ("consumer", "development") if collection == "all" else (collection,)
    )

    skills_root = resources.files("mcp_semantic_gateway").joinpath("skills")
    for sub in targets:
        coll_dir = skills_root.joinpath(sub)
        if not coll_dir.is_dir():
            continue
        for skill_dir in sorted(coll_dir.iterdir(), key=lambda p: p.name):
            if not skill_dir.is_dir():
                continue
            if not skill_dir.joinpath("SKILL.md").is_file():
                continue
            yield _BundledSkill(collection=sub, name=skill_dir.name, source=skill_dir)


def _copy_traversable(src: Traversable, dest: Path) -> None:
    """Recursively copy an ``importlib.resources`` traversable to a real dir."""

    dest.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dest / entry.name
        if entry.is_dir():
            _copy_traversable(entry, target)
        else:
            with entry.open("rb") as fh:
                data = fh.read()
            target.write_bytes(data)


def _install_skill(skill: _BundledSkill, target_root: Path, *, force: bool) -> str:
    """Copy one bundled skill into ``target_root/<skill.name>/``.

    Returns one of ``"installed"``, ``"overwritten"``, ``"skipped"``.
    """

    target = target_root / skill.name
    if target.exists():
        if not force:
            return "skipped"
        shutil.rmtree(target)
        _copy_traversable(skill.source, target)
        return "overwritten"
    _copy_traversable(skill.source, target)
    return "installed"


def _validate_provider(provider: str) -> ProviderTarget:
    try:
        return PROVIDERS[provider]
    except KeyError:
        valid = ", ".join(sorted(PROVIDERS))
        raise typer.BadParameter(
            f"unknown provider {provider!r}; expected one of {valid}"
        )


def onboard(
    provider: str = typer.Argument(
        None,
        help=(
            "Coding agent to onboard. One of: "
            + ", ".join(sorted(PROVIDERS))
            + "."
        ),
    ),
    collection: str = typer.Option(
        "all",
        "--include",
        "-i",
        help="Which bundled skill collection to install: consumer, development, or all.",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        help="Install into the project-local skills dir (cwd) instead of the user-level dir.",
    ),
    target: Path = typer.Option(
        None,
        "--target",
        help="Override the destination root directly (skills are placed under this path).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing skill directories that have the same name.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the plan without writing anything.",
    ),
    list_providers: bool = typer.Option(
        False,
        "--list-providers",
        help="Show the supported providers + the directories the gateway writes to, then exit.",
    ),
    list_skills: bool = typer.Option(
        False,
        "--list-skills",
        help="Show the bundled skills (consumer + development) shipped with this version.",
    ),
) -> None:
    """Install bundled skills into a coding agent's skills directory."""

    if list_providers:
        _render_provider_table()
        raise typer.Exit()

    if list_skills:
        _render_skills_table()
        raise typer.Exit()

    if provider is None:
        _render_provider_table()
        raise typer.Exit(code=2)

    if collection not in COLLECTIONS:
        valid = ", ".join(COLLECTIONS)
        raise typer.BadParameter(
            f"--include must be one of {valid}; got {collection!r}"
        )

    provider_target = _validate_provider(provider)
    if target is not None:
        dest_root = target.expanduser().resolve()
    else:
        dest_root = provider_target.resolve(project=project)
        if not project:
            dest_root = dest_root.expanduser()

    skills = list(iter_bundled_skills(collection))
    if not skills:
        typer.secho(
            f"No bundled skills found for collection {collection!r}. "
            "Has the package been installed correctly?",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    console = Console()
    scope = "project" if project else "user"
    if target is not None:
        scope = "custom"
    console.print(
        f"[bold]Onboarding[/bold] {provider_target.name} "
        f"([dim]{scope}[/dim]) -> [cyan]{dest_root}[/cyan]"
    )
    console.print(f"[dim]{provider_target.notes}[/dim]")

    if dry_run:
        console.print("[yellow]--dry-run set; no files will be written.[/yellow]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Collection")
    table.add_column("Skill")
    table.add_column("Action")

    counts = {"installed": 0, "overwritten": 0, "skipped": 0, "would-install": 0}
    for skill in skills:
        if dry_run:
            target_dir = dest_root / skill.name
            if target_dir.exists() and force:
                action = "would-overwrite"
            elif target_dir.exists():
                action = "would-skip"
            else:
                action = "would-install"
                counts["would-install"] += 1
        else:
            action = _install_skill(skill, dest_root, force=force)
            counts[action] = counts.get(action, 0) + 1
        table.add_row(skill.collection, skill.name, action)

    console.print(table)

    if dry_run:
        console.print(
            f"[green]Plan: {counts['would-install']} new skill(s) would be written "
            f"into {dest_root}.[/green]"
        )
    else:
        console.print(
            f"[green]Done.[/green] installed={counts.get('installed', 0)} "
            f"overwritten={counts.get('overwritten', 0)} "
            f"skipped={counts.get('skipped', 0)}"
        )
        if counts.get("skipped", 0) and not force:
            console.print(
                "[dim]Tip: re-run with [bold]--force[/bold] to overwrite existing skills.[/dim]"
            )


def _render_skills_table() -> None:
    """List the bundled skills shipped with this version of the gateway."""

    console = Console()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Collection")
    table.add_column("Skill")
    table.add_column("Description")

    for skill in iter_bundled_skills("all"):
        desc = _read_description(skill.source)
        table.add_row(skill.collection, skill.name, desc)

    console.print(table)


def _read_description(skill_dir: Traversable) -> str:
    """Pull the first ``description:`` line out of a bundled SKILL.md."""

    skill_md = skill_dir.joinpath("SKILL.md")
    try:
        with skill_md.open("r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return ""

    in_frontmatter = False
    for line in text.splitlines():
        if line.strip() == "---":
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break
        if in_frontmatter and line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def _render_provider_table() -> None:
    """Print the supported providers and where ``onboard`` writes for each."""

    console = Console()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("User-level path")
    table.add_column("Project-level path")
    table.add_column("Notes")

    for key in sorted(PROVIDERS):
        target = PROVIDERS[key]
        table.add_row(
            target.name,
            str(target.user_dir),
            str(target.project_dir),
            target.notes,
        )

    console.print("[bold]Supported coding agents:[/bold]")
    console.print(table)
    console.print(
        "[dim]Run `mcp-semantic-gateway onboard <provider>` to install bundled skills.[/dim]"
    )
