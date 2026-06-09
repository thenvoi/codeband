"""Codeband CLI — multi-agent coding orchestration via Band.ai."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import click
from dotenv import find_dotenv, load_dotenv

from codeband.config import (
    AgentsConfig,
    CodebandConfig,
    DeploymentMode,
    Framework,
    RepoConfig,
    load_config,
)
from codeband.orchestration.compose import (
    ComposeFileNotFound,
    find_compose_file as _find_compose_file,
)


def _load_project_dotenv(project_dir: str) -> None:
    """Load ``.env`` from the project dir if present, else fall back to CWD search.

    ``cb --dir /some/project`` from a different CWD should still pick up
    that project's ``.env``. ``find_dotenv(usecwd=True)`` only searches
    upward from CWD, so we check the explicit project dir first. Default
    non-overriding behavior is preserved — variables already set in the
    parent shell win.
    """
    project = Path(project_dir).resolve()
    project_env = project / ".env"
    if project_env.is_file():
        load_dotenv(project_env)
        return
    load_dotenv(find_dotenv(usecwd=True))


def _init_project_env(project_dir: str) -> None:
    """Load ``.env`` for a project dir then re-resolve LLM auth.

    Call this at the top of every subcommand body that takes ``--dir``.
    The cli group also calls it, but only on the bare-cb path — when a
    subcommand runs, the group's project_dir is "." (the default) and
    only the subcommand sees the user's actual ``--dir``.
    """
    _load_project_dotenv(project_dir)
    _resolve_claude_auth(project_dir)
    _resolve_codex_auth()


def _project_aware(fn):
    """Decorator: run :func:`_init_project_env` before the wrapped command.

    Click invokes the decorated function with all options bound by name,
    so we read ``project_dir`` from kwargs. Idempotent — if the group
    already loaded the same .env, ``load_dotenv`` is a no-op for vars
    already set.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        project_dir = kwargs.get("project_dir", ".")
        _init_project_env(project_dir)
        return fn(*args, **kwargs)
    return wrapper


def _run_async(coro):
    """Run an async coroutine with user-friendly error handling for Band.ai API calls."""
    try:
        return asyncio.run(coro)
    except RuntimeError as e:
        raise click.ClickException(str(e)) from None
    except Exception as e:
        err_type = type(e).__name__.lower()
        err_str = str(e).lower()
        if "unauthorized" in err_type or "unauthorized" in err_str or "401" in err_str:
            raise click.ClickException(
                "Band.ai API authentication failed.\n"
                "  - Is BAND_API_KEY set in .env?\n"
                "  - Have you registered agents? Run: cb setup-agents\n"
                "  - Does the platform URL in codeband.yaml match your API key?"
            ) from None
        if "connect" in err_type or "connection" in err_type:
            raise click.ClickException(
                f"Cannot connect to Band.ai platform: {e}\n"
                "Check your network and band.rest_url in codeband.yaml."
            ) from None
        raise click.ClickException(str(e)) from None


def _has_claude_subscription_oauth() -> bool:
    """True if a Claude Code subscription OAuth credential is available locally.

    Per the Claude Code auth docs, the CLI stores subscription OAuth either in:
    - macOS Keychain (service ``Claude Code-credentials``); or
    - ``$CLAUDE_CODE_CONFIG_DIR/.credentials.json`` (Linux/Windows, honoring
      ``CLAUDE_CONFIG_DIR``; default ``~/.claude/.credentials.json``).

    The bundled Claude CLI reads whichever is present when no higher-precedence
    env var (``ANTHROPIC_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``) is set. Docker
    containers have no keychain and typically no credentials file unless
    explicitly bind-mounted, so this generally returns False there — the
    container must rely on env-var auth.
    """
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials"],
                capture_output=True,
                check=False,
                timeout=2,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        else:
            if result.returncode == 0:
                return True
        # Fall through to file check — possible on macOS if user has overridden
        # with CLAUDE_CONFIG_DIR or uses the file-based backend.
    config_dir_env = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(config_dir_env) if config_dir_env else Path.home() / ".claude"
    return (config_dir / ".credentials.json").is_file()




def _claude_auth_mode(project_dir: str) -> str:
    """Read ``claude.auth_mode`` from ``codeband.yaml``; default ``"api_key"``.

    Auth resolves at CLI entry, before the full config is loaded, and
    ``codeband.yaml`` may not exist yet (e.g. during ``cb init``). Read just
    this one field defensively with a direct ``yaml.safe_load`` — any
    missing-file / parse / unknown-value case falls back to ``"api_key"`` (the
    safe, compliant default). We deliberately avoid ``load_config`` here: it
    raises on a missing file and strict-validates the whole document, and auth
    resolution must not crash the CLI over an unrelated config error.
    """
    config_path = Path(project_dir) / "codeband.yaml"
    if not config_path.is_file():
        return "api_key"
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        mode = (data.get("claude") or {}).get("auth_mode", "api_key")
    except (OSError, yaml.YAMLError, AttributeError):
        return "api_key"
    return mode if mode in ("api_key", "subscription") else "api_key"


def _resolve_claude_auth(project_dir: str) -> None:
    """Resolve Claude auth per ``claude.auth_mode`` (default ``"api_key"``).

    ``api_key`` (default): leave the environment alone. The Claude CLI's native
    precedence uses ``ANTHROPIC_API_KEY`` — Anthropic's Commercial Terms, the
    supported path for automated/parallel agents. Subscription OAuth is never
    taken implicitly; ``cb run`` preflight fails fast when no key is present
    (see ``preflight.check_claude_auth``) so the ToS-restricted subscription
    path is never used by accident.

    ``subscription`` (explicit opt-in): prefer Claude Pro/Max OAuth. The Claude
    CLI would otherwise put ``ANTHROPIC_API_KEY`` *above* subscription OAuth, so
    when both are present we strip the key — keeping a process-local backup so
    preflight can fall back only on a subscription usage-limit exhaustion:
      1. ``CLAUDE_CODE_OAUTH_TOKEN`` env var, or
      2. subscription credential on disk / keychain (see
         ``_has_claude_subscription_oauth``).

    Mirrored in ``docker/entrypoint.sh``, which reads the same config field.
    Subscription-credential detection is host-only — containers generally
    can't access host keychains or ``~/.claude``.
    """
    if _claude_auth_mode(project_dir) != "subscription":
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return
    if (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or _has_claude_subscription_oauth()
    ):
        os.environ.setdefault(
            "CODEBAND_FALLBACK_ANTHROPIC_API_KEY",
            os.environ["ANTHROPIC_API_KEY"],
        )
        del os.environ["ANTHROPIC_API_KEY"]


def _codex_auth_file() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return codex_home / "auth.json"


def _has_codex_subscription_auth() -> bool:
    auth_file = _codex_auth_file()
    if not auth_file.is_file():
        return False
    try:
        content = auth_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return '"auth_mode": "ChatGPT"' in content


def _resolve_codex_auth() -> None:
    """Prefer Codex ChatGPT subscription auth over API key at startup.

    Like Claude auth, Codeband should not silently burn API credits while the
    subscription path is usable. If ``OPENAI_API_KEY`` is set alongside a
    logged-in Codex ChatGPT subscription, strip the key from the active
    environment but keep a process-local fallback. Preflight restores it only
    if the subscription path reports usage-limit exhaustion.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return
    if _has_codex_subscription_auth():
        os.environ.setdefault(
            "CODEBAND_FALLBACK_OPENAI_API_KEY",
            os.environ["OPENAI_API_KEY"],
        )
        del os.environ["OPENAI_API_KEY"]


@click.group(invoke_without_command=True)
@click.option(
    "--dir", "project_dir", default=".",
    help="Project directory for the interactive shell (subcommands have their own --dir).",
)
@click.option(
    "--attach", is_flag=True, default=False,
    help="Attach to an existing orchestrator instead of starting one in-process. "
         "Useful when agents already run in Docker / on remote hosts. "
         "Also enabled when CODEBAND_SHELL_ATTACH=1 is set.",
)
@click.option(
    "--skip-preflight", is_flag=True, default=False,
    help="Skip the Claude/Codex auth preflight (advanced; use for offline/CI).",
)
@click.version_option()
@click.pass_context
def cli(
    ctx: click.Context,
    project_dir: str,
    attach: bool,
    skip_preflight: bool,
) -> None:
    """Codeband — multi-agent coding orchestration via Band.ai.

    Run ``cb`` with no subcommand from a TTY to open the interactive
    shell (single terminal: orchestrator + live feed + slash prompt).
    Pipe stdin or stdout (e.g. CI) to fall through to ``cb --help``.

    The ``--dir``, ``--attach`` and ``--skip-preflight`` options here only
    apply to that interactive entry — each subcommand (``cb run``,
    ``cb diff``, ...) accepts its own flags and loads its own ``.env``.
    """
    if ctx.invoked_subcommand is not None:
        # Each subcommand has its own --dir and runs ``_init_project_env``
        # itself via the @_project_aware decorator. Doing it here too
        # would lock in the wrong .env when CWD differs from --dir.
        return

    _init_project_env(project_dir)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        click.echo(ctx.get_help())
        return

    attach_mode = attach or os.environ.get("CODEBAND_SHELL_ATTACH") == "1"

    from codeband.shell.repl import run as run_shell
    run_shell(project_dir, attach=attach_mode, skip_preflight=skip_preflight)


@cli.command()
@click.option("--repo", required=True, help="Git repository URL to work on")
@click.option("--branch", default="main", help="Branch to base work on")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def init(repo: str, branch: str, project_dir: str) -> None:
    """Initialize a Codeband project.

    The default config uses 8 Band.ai agents (fits the free-tier 10-cap):
    1 Claude coder + 1 Codex coder + 1 reviewer of each framework +
    1 Claude planner + 1 Codex plan-reviewer + conductor + mergemaster.

    To scale up on paid tier: edit `codeband.yaml` or use `cb scale`.
    """
    project = Path(project_dir).resolve()
    project.mkdir(parents=True, exist_ok=True)

    config = CodebandConfig(
        repo=RepoConfig(url=repo, branch=branch),
        agents=AgentsConfig(),  # cross-model defaults from config.py
    )
    config_path = project / "codeband.yaml"
    config.to_yaml(config_path)
    click.echo(f"Created {config_path}")

    # Create .env.example
    env_example = project / ".env.example"
    env_example.write_text(
        "# Codeband environment variables\n"
        "#\n"
        "# ── Claude authentication ───────────────────────────────────────\n"
        "# Used by every Claude-based agent (Conductor, Planner, Coders,\n"
        "# Reviewers, Mergemaster) and the cb prs/issues --smart helpers.\n"
        "#\n"
        "# Codeband defaults to API-key auth (claude.auth_mode: api_key in\n"
        "# codeband.yaml). The Anthropic API (Commercial Terms) is the supported\n"
        "# path for automated, parallel agents.\n"
        "ANTHROPIC_API_KEY=sk-ant-...\n"
        "#\n"
        "# To deliberately bill a Claude Pro/Max subscription instead, set\n"
        "#   claude:\n"
        "#     auth_mode: subscription\n"
        "# in codeband.yaml, then provide a subscription credential below.\n"
        "# NOTE: Anthropic's Consumer Terms restrict automated subscription use\n"
        "# (see docs/AUTHENTICATION.md) — this is an explicit opt-in.\n"
        "#   - CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) — required for\n"
        "#     Docker/CI where the host keychain isn't accessible:\n"
        "# CLAUDE_CODE_OAUTH_TOKEN=...\n"
        "#   - or, on your own host, run `claude` login and Codeband picks up the\n"
        "#     subscription automatically (macOS Keychain or ~/.claude).\n"
        "#\n"
        "# ── Codex authentication (optional, for Codex agents) ───────────\n"
        "# Codeband starts with ChatGPT subscription auth when available\n"
        "# (`codex login --device-auth`, stored in ~/.codex/auth.json).\n"
        "# If OPENAI_API_KEY is also set, it is used only after Codex reports\n"
        "# a subscription usage-limit error. If you don't use Codex agents,\n"
        "# leave this unset.\n"
        "OPENAI_API_KEY=sk-...\n"
        "#\n"
        "# ── Platform & GitHub ───────────────────────────────────────────\n"
        "BAND_API_KEY=band_u_...\n"
        "GH_TOKEN=ghp_...\n"
        "#\n"
        "# ── Memory backend override (optional) ──────────────────────────\n"
        "# Codeband auto-detects Band.ai memory availability at startup.\n"
        "# Set this to force a specific backend (useful for CI or debugging).\n"
        "# BAND_MEMORY_MODE=auto   # auto (default) | band | local\n"
    )
    click.echo(f"Created {env_example}")

    # Ensure .codeband/ is gitignored in the project
    gitignore = project / ".gitignore"
    marker = ".codeband/"
    if gitignore.exists():
        content = gitignore.read_text()
        if marker not in content.splitlines():
            gitignore.write_text(content.rstrip("\n") + f"\n{marker}\n")
            click.echo(f"Added {marker} to .gitignore")
    else:
        gitignore.write_text(f"{marker}\n")
        click.echo(f"Created .gitignore with {marker}")

    click.echo("\nNext steps:")
    click.echo("  1. cp .env.example .env && edit .env with your API keys")
    click.echo("  2. codeband setup-agents       # Register agents on Band.ai")
    click.echo("  3. cb                          # Open interactive shell (one terminal)")
    click.echo("     cb up                       # Docker mode (auto-attaches shell)")
    click.echo("     cb run                      # Headless orchestrator (CI / scripts)")


@cli.command()
@click.argument("spec")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def scale(spec: str, project_dir: str, command_style: str = "cli") -> None:
    """Scale a worker-pool entry.

    SPEC format: `<pool>.<framework>=<count>`

    Examples:
      cb scale coders.claude_sdk=4        # 4 Claude coders
      cb scale coders.codex=0             # opt out of Codex coders
      cb scale reviewers.claude_sdk=2     # 2 Claude reviewers

    Valid pools: planners, plan_reviewers, coders, reviewers.
    Valid frameworks: claude_sdk, codex. Then run `cb setup-agents` to
    register any new pool identities.
    """
    from codeband.config import scale_pool

    try:
        path, raw_count = spec.split("=", 1)
        pool, framework_str = path.split(".", 1)
        count = int(raw_count)
    except ValueError:
        click.echo(
            f"Error: bad spec '{spec}'. Expected '<pool>.<framework>=<count>' "
            "(e.g. coders.claude_sdk=4).",
            err=True,
        )
        sys.exit(1)

    try:
        framework = Framework(framework_str)
    except ValueError:
        click.echo(
            f"Error: unknown framework '{framework_str}'. Use claude_sdk or codex.",
            err=True,
        )
        sys.exit(1)

    project = Path(project_dir).resolve()
    config_path = project / "codeband.yaml"
    if not config_path.exists():
        click.echo("Error: codeband.yaml not found. Run 'cb init' first.", err=True)
        sys.exit(1)

    try:
        config = scale_pool(config_path, pool, framework, count)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"Scaled {pool}.{framework.value} to {count}.")
    click.echo(f"Total Band.ai agents: {config.agents.total_agent_count()}")
    if config.agents.total_agent_count() > 10:
        click.echo(
            "Warning: agent count > 10 exceeds Band.ai free-tier cap.",
            err=True,
        )
    # The slash handler (`/scale`) prints its own context-aware next steps
    # (docker rebuild vs shell restart), so only emit the cli-worded block
    # when invoked as `cb scale`.
    if command_style == "cli":
        click.echo("\nNext steps:")
        click.echo("  cb setup-agents   # Register any new pool identities")
        click.echo("  cb                # Restart the interactive shell to pick up changes")


@cli.command("setup-agents")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def setup_agents(project_dir: str) -> None:
    """Register agents on the Band.ai platform and write credentials."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.orchestration.setup import register_all_agents

    _run_async(register_all_agents(config, project))


@cli.command()
@click.option("--agent", default=None, help="Run a single agent by key (distributed mode)")
@click.option("--debug", is_flag=True, help="Enable verbose debug logging")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@click.option(
    "--skip-preflight", is_flag=True,
    help="Skip the Claude auth preflight check (advanced; use for offline/CI).",
)
@_project_aware
def run(agent: str | None, debug: bool, project_dir: str, skip_preflight: bool) -> None:
    """Run agents locally.

    Without --agent: runs all agents in-process (local mode).
    With --agent <key>: runs a single agent (distributed mode).
    """
    project = Path(project_dir).resolve()
    config = load_config(project)

    log_level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    # Suppress noisy third-party loggers unless --debug
    if not debug:
        logging.getLogger("phoenix_channels_python_client").setLevel(logging.ERROR)
        logging.getLogger("asyncio").setLevel(logging.ERROR)

    if not skip_preflight:
        from codeband.logging_setup import suppress_preflight_sdk_noise
        from codeband.preflight import run_preflight

        # The SDK logs ``Fatal error in message reader: Command failed …``
        # at ERROR level for every non-zero CLI exit. Outside preflight
        # that's a load-bearing failure signal (see logging_setup.py),
        # but here we already classify and print the failure cleanly, so
        # the log line is pure noise. Scope the suppression to just this
        # call.
        with suppress_preflight_sdk_noise():
            err = asyncio.run(run_preflight(config))
        if err is not None:
            # Classified failures: the remediation already names what
            # went wrong. The summary just dumps SDK exception text and
            # structured context — useful for --debug, never for users.
            if err.classified:
                raise click.ClickException(err.remediation)
            raise click.ClickException(f"{err.summary}\n\n{err.remediation}")

    if agent:
        click.echo(f"Starting agent {agent}... (Ctrl+C to stop)")
        from codeband.orchestration.runner import run_agent
        _run_async(run_agent(config, project, agent))
    else:
        if config.workspace.mode == DeploymentMode.DISTRIBUTED:
            click.echo(
                "Warning: workspace.mode is 'distributed' but running all agents locally. "
                "Use --agent <key> to run a single agent, or set mode to 'local'.",
                err=True,
            )
        total = config.agents.total_agent_count()
        click.echo(f"Starting Codeband with {total} agents... (Ctrl+C to stop)")
        from codeband.orchestration.runner import run_local
        _run_async(run_local(config, project))

    click.echo("All agents stopped.")


@cli.command()
@click.argument("description")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def task(description: str, project_dir: str) -> None:
    """Send a task to the Conductor."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.orchestration.kickoff import send_task

    _run_async(send_task(config, project, description))


@cli.command()
@click.option("--sort", "sort_mode", default="newest",
              type=click.Choice(["newest", "oldest", "smallest", "largest", "most-discussed"]),
              help="Sort order for PRs")
@click.option("--smart", is_flag=True, help="AI-rank PRs by estimated impact")
@click.option("--limit", default=5, type=int, help="Number of PRs to show")
@click.option("--pick", default=None, type=int, help="Skip menu, use PR #N directly")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def prs(sort_mode: str, smart: bool, limit: int, pick: int | None,
        project_dir: str) -> None:
    """Browse open PRs and send one as a task."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.github.prs import (
        PRInfo,
        fetch_open_prs,
        repo_slug,
        smart_rank,
        sort_prs,
    )

    slug = repo_slug(config.repo.url)
    click.echo(f"Fetching open PRs from {slug}...")

    raw = fetch_open_prs(slug, limit=100)
    if not raw:
        click.echo("No open PRs found.")
        return

    prs_list = [PRInfo.from_gh(p) for p in raw]

    if pick is not None:
        chosen = next((pr for pr in prs_list if pr.number == pick), None)
        if not chosen:
            click.echo(f"PR #{pick} not found among open PRs.", err=True)
            sys.exit(1)
    elif smart:
        click.echo("Ranking PRs by impact (AI)...")
        ranked = _run_async(smart_rank(prs_list, limit=limit))
        click.echo(f"\nTop {len(ranked)} PRs by impact:\n")
        for i, r in enumerate(ranked, 1):
            click.echo(f"  {i}. #{r.number} {r.title}")
            click.echo(f"     → {r.reason}\n")
        choice = click.prompt("Pick a PR number to send as task (0 to cancel)", type=int)
        if choice == 0:
            return
        chosen = next((pr for pr in prs_list if pr.number == choice), None)
        if not chosen:
            click.echo(f"PR #{choice} not in list.", err=True)
            sys.exit(1)
    else:
        sorted_prs = sort_prs(prs_list, sort_mode)[:limit]
        click.echo(f"\nOpen PRs (sorted by {sort_mode}):\n")
        for pr in sorted_prs:
            click.echo(f"  {pr.summary_line(slug)}")
        click.echo()
        choice = click.prompt("Pick a PR number to send as task (0 to cancel)", type=int)
        if choice == 0:
            return
        chosen = next((pr for pr in prs_list if pr.number == choice), None)
        if not chosen:
            click.echo(f"PR #{choice} not in list.", err=True)
            sys.exit(1)

    # Build task description from the PR
    description = (
        f"Work on PR #{chosen.number}: {chosen.title}\n\n"
        f"Author: {chosen.author}\n"
        f"Labels: {', '.join(chosen.labels) or 'none'}\n"
        f"Size: +{chosen.additions}/-{chosen.deletions} across {chosen.changed_files} files\n\n"
        f"Review, complete, or advance this pull request."
    )
    click.echo(f"\nSending task for PR #{chosen.number}: {chosen.title}")

    from codeband.orchestration.kickoff import send_task

    _run_async(send_task(config, project, description))


def _build_issue_task(issue_info, *, auto: bool = False) -> str:
    """Build a task description from a GitHub issue."""
    body_excerpt = issue_info.body[:500] if issue_info.body else "(no description)"
    action = (
        "Please analyze this issue and implement a fix."
        if auto
        else "Please review this issue, propose a plan, and wait for approval before implementing."
    )
    return (
        f"GitHub issue #{issue_info.number}: {issue_info.title}\n\n"
        f"{body_excerpt}\n\n"
        f"Labels: {', '.join(issue_info.labels) or 'none'}\n"
        f"Author: {issue_info.author}\n\n"
        f"{action}"
    )


@cli.command()
@click.option("--sort", "sort_mode", default="newest",
              type=click.Choice(["newest", "oldest", "most-discussed"]),
              help="Sort order for issues")
@click.option("--smart", is_flag=True, help="AI-rank issues by estimated impact")
@click.option("--limit", default=5, type=int, help="Number of issues to show")
@click.option("--pick", default=None, type=int, help="Skip menu, send issue #N directly")
@click.option("--label", default=None, help="Filter by label (e.g., 'bug')")
@click.option("--auto", is_flag=True, help="Skip plan approval, go straight to implementation")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def issues(sort_mode: str, smart: bool, limit: int, pick: int | None,
           label: str | None, auto: bool, project_dir: str) -> None:
    """Browse open issues and send one as a task."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.github.issues import (
        IssueInfo,
        fetch_issue_detail,
        fetch_open_issues,
        smart_rank,
        sort_issues,
    )
    from codeband.github.prs import repo_slug

    slug = repo_slug(config.repo.url)
    click.echo(f"Fetching open issues from {slug}...")

    raw = fetch_open_issues(slug, limit=100, label=label)
    if not raw:
        click.echo("No open issues found.")
        return

    issues_list = [IssueInfo.from_gh(i) for i in raw]

    if pick is not None:
        chosen = next((i for i in issues_list if i.number == pick), None)
        if not chosen:
            click.echo(f"Issue #{pick} not found among open issues.", err=True)
            sys.exit(1)
        # Fetch full body for the picked issue
        detail = fetch_issue_detail(slug, pick)
        chosen = IssueInfo.from_gh(detail)
    elif smart:
        click.echo("Ranking issues by impact (AI)...")
        ranked = _run_async(smart_rank(issues_list, limit=limit))
        click.echo(f"\nTop {len(ranked)} issues by impact:\n")
        for idx, r in enumerate(ranked, 1):
            click.echo(f"  {idx}. #{r.number} {r.title}")
            click.echo(f"     → {r.reason}\n")
        choice = click.prompt("Pick an issue number to send as task (0 to cancel)", type=int)
        if choice == 0:
            return
        detail = fetch_issue_detail(slug, choice)
        chosen = IssueInfo.from_gh(detail)
    else:
        sorted_issues = sort_issues(issues_list, sort_mode)[:limit]
        click.echo(f"\nOpen issues (sorted by {sort_mode}):\n")
        for i in sorted_issues:
            click.echo(f"  {i.summary_line(slug)}")
        click.echo()
        choice = click.prompt("Pick an issue number to send as task (0 to cancel)", type=int)
        if choice == 0:
            return
        detail = fetch_issue_detail(slug, choice)
        chosen = IssueInfo.from_gh(detail)

    description = _build_issue_task(chosen, auto=auto)
    click.echo(f"\nSending task for issue #{chosen.number}: {chosen.title}")

    from codeband.orchestration.kickoff import send_task

    _run_async(send_task(config, project, description))


@cli.command()
@click.argument("number", type=int)
@click.option("--auto", is_flag=True, help="Skip plan approval, go straight to implementation")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def issue(number: int, auto: bool, project_dir: str) -> None:
    """Send a specific GitHub issue to agents for implementation."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.github.issues import IssueInfo, fetch_issue_detail
    from codeband.github.prs import repo_slug

    slug = repo_slug(config.repo.url)
    click.echo(f"Fetching issue #{number} from {slug}...")

    detail = fetch_issue_detail(slug, number)
    chosen = IssueInfo.from_gh(detail)

    description = _build_issue_task(chosen, auto=auto)
    click.echo(f"\nSending task for issue #{chosen.number}: {chosen.title}")

    from codeband.orchestration.kickoff import send_task

    _run_async(send_task(config, project, description))


@cli.command()
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def status(project_dir: str) -> None:
    """Query task status from Band.ai memory."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.orchestration.kickoff import query_status

    _run_async(query_status(config, project))


def _resolve_worker_id(candidate: str, candidates: dict[str, Path]) -> str:
    """Resolve a user-typed worker name to a canonical worker_id.

    Tries case-insensitive exact match first, then case-insensitive substring
    match restricted to a single candidate. Raises click.UsageError on no-match
    or ambiguous-match, listing available worker_ids in the error text.
    """
    keys = list(candidates.keys())
    lc = candidate.lower()

    # Exact match (case-insensitive)
    for k in keys:
        if k.lower() == lc:
            return k

    # Substring match (case-insensitive)
    hits = [k for k in keys if lc in k.lower()]
    if len(hits) == 1:
        return hits[0]

    available = "\n".join(f"  {k}" for k in sorted(keys))
    if not hits:
        raise click.UsageError(
            f"No worker matches '{candidate}'.\nAvailable workers:\n{available}"
        )
    ambiguous = "\n".join(f"  {k}" for k in sorted(hits))
    raise click.UsageError(
        f"'{candidate}' is ambiguous — matches multiple workers:\n{ambiguous}"
    )


@cli.command(name="diff")
@click.argument("worker", required=False)
@click.option("--patch", "-p", is_flag=True, help="Show full unified diff instead of summary.")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def diff_cmd(worker: str | None, patch: bool, project_dir: str) -> None:
    """Show what a worker has changed since forking from the base branch."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.shell.fs import make_backend
    from codeband.workspace.diff import DiffError

    backend = make_backend(config, project)
    candidates = backend.list_worktrees()

    if not candidates:
        raise click.ClickException(
            "No coder or mergemaster worktrees are configured — run `cb init` first."
        )

    if not worker:
        available = "\n".join(f"  {k}" for k in sorted(candidates))
        click.echo(f"Usage: cb diff <worker>\n\nAvailable workers:\n{available}", err=True)
        sys.exit(1)

    resolved = _resolve_worker_id(worker, candidates)

    try:
        wd = backend.worktree_diff(resolved, config.repo.branch, include_patch=patch)
    except DiffError as e:
        raise click.ClickException(str(e)) from None

    click.echo(
        f"Worker: {wd.worker_id}\n"
        f"Worktree: {wd.worktree}\n"
        f"Base: {wd.base_ref} (fork-point {wd.merge_base[:12]})"
    )
    click.echo("")

    if not wd.has_changes:
        click.echo(f"No changes yet on {wd.worker_id}.")
        return

    if patch:
        if wd.patch:
            click.echo(wd.patch, nl=False)
        else:
            click.echo("(no committed or staged changes)")
    else:
        if wd.stat:
            click.echo(wd.stat)
        else:
            click.echo("(no committed or staged changes)")

    if wd.untracked:
        click.echo("")
        click.echo("Untracked files:")
        for f in wd.untracked:
            click.echo(f"  {f}")


@cli.command()
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def doctor(project_dir: str) -> None:
    """Check environment, config, and connectivity."""
    project = Path(project_dir).resolve()

    from codeband.doctor import report, run_all

    ctx, exit_code = asyncio.run(run_all(project))
    report(ctx)
    sys.exit(exit_code)


@cli.command()
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def pending(project_dir: str, command_style: str = "cli") -> None:
    """Show PRs with risk classification and merge eligibility."""
    import json
    import re
    import subprocess

    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.github.prs import pr_url, repo_slug

    slug = repo_slug(config.repo.url)
    policy = config.agents.mergemaster.auto_merge.value

    # Risk level ordering for policy comparison
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    policy_threshold = risk_order.get(policy, -1)  # "all" → -1, "none" → always block

    # Fetch open PRs with comments (to find risk classifications)
    cmd = [
        "gh", "pr", "list", "--repo", slug, "--state", "open",
        "--json", "number,title,labels,comments",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        click.echo("Error: gh CLI not installed.", err=True)
        sys.exit(1)
    if result.returncode != 0:
        click.echo(f"Error: {result.stderr.strip()}", err=True)
        sys.exit(1)

    prs = json.loads(result.stdout)
    if not prs:
        click.echo("No open PRs found.")
        return

    # Extract risk level from PR comments (Reviewer posts "risk: <level>")
    risk_re = re.compile(r"risk:\s*(low|medium|high|critical)", re.IGNORECASE)

    click.echo()
    click.echo("=" * 80)
    click.echo(f"  OPEN PRs  (auto_merge policy: {policy})")
    click.echo("=" * 80)
    needs_approval = []
    auto_eligible = []
    unreviewed = []

    for p in prs:
        number = p["number"]
        title = p["title"]
        link = pr_url(slug, number)
        labels = [lb["name"] for lb in p.get("labels", [])]

        # Search comments for risk classification
        risk = None
        comments = p.get("comments", [])
        for comment in comments:
            body = comment.get("body", "") if isinstance(comment, dict) else ""
            m = risk_re.search(body)
            if m:
                risk = m.group(1).lower()

        if risk is None:
            unreviewed.append((number, title, link, labels))
        elif policy == "all" or risk_order.get(risk, 99) <= policy_threshold:
            auto_eligible.append((number, title, risk, link, labels))
        else:
            needs_approval.append((number, title, risk, link, labels))

    if needs_approval:
        click.echo("\n  AWAITING HUMAN APPROVAL:")
        for number, title, risk, link, labels in needs_approval:
            label_str = f" [{', '.join(labels)}]" if labels else ""
            click.echo(f"    #{number:<6} {title[:40]:<40}  risk: {risk:<10}{label_str}")
            click.echo(f"           {link}")

    if auto_eligible:
        click.echo("\n  AUTO-MERGE ELIGIBLE:")
        for number, title, risk, link, labels in auto_eligible:
            label_str = f" [{', '.join(labels)}]" if labels else ""
            click.echo(f"    #{number:<6} {title[:40]:<40}  risk: {risk:<10}{label_str}")
            click.echo(f"           {link}")

    if unreviewed:
        click.echo("\n  NOT YET REVIEWED:")
        for number, title, link, labels in unreviewed:
            label_str = f" [{', '.join(labels)}]" if labels else ""
            click.echo(f"    #{number:<6} {title[:40]:<40}  {label_str}")
            click.echo(f"           {link}")

    if policy == "none":
        click.echo("\n  Policy: none — all PRs require human approval.")
    click.echo()
    if command_style == "slash":
        approve_hint = "/approve <number>"
        reject_hint = "/reject <number> --reason \"...\""
    else:
        approve_hint = "cb approve <number>"
        reject_hint = "cb reject <number> --reason \"...\""
    click.echo(f"  Approve:  {approve_hint}")
    click.echo(f"  Reject:   {reject_hint}")
    click.echo("=" * 80)
    click.echo()


@cli.command()
@click.argument("number", type=int)
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def approve(number: int, project_dir: str, command_style: str = "cli") -> None:
    """Approve a PR for merge (sends approval to Conductor in existing task room)."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.github.prs import pr_url, repo_slug

    slug = repo_slug(config.repo.url)
    link = pr_url(slug, number)

    message = (
        f"APPROVED: Please merge PR #{number}. {link}\n"
        f"Human has reviewed and approved this PR for merge."
    )
    click.echo(f"Approving PR #{number}: {link}")

    from codeband.orchestration.kickoff import send_room_message

    _run_async(send_room_message(
        config, project, message, command_style=command_style,
    ))


@cli.command()
@click.argument("number", type=int)
@click.option("--reason", default=None, help="Reason for rejection")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def reject(
    number: int,
    reason: str | None,
    project_dir: str,
    command_style: str = "cli",
) -> None:
    """Reject a PR (sends rejection to Conductor in existing task room)."""
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.github.prs import pr_url, repo_slug

    slug = repo_slug(config.repo.url)
    link = pr_url(slug, number)

    reason_text = f" Reason: {reason}" if reason else ""
    message = (
        f"REJECTED: Do NOT merge PR #{number}.{reason_text} {link}\n"
        f"Please address the feedback or close the PR."
    )
    click.echo(f"Rejecting PR #{number}: {link}")

    from codeband.orchestration.kickoff import send_room_message

    _run_async(send_room_message(
        config, project, message, command_style=command_style,
    ))


@cli.command()
@click.option("--dir", "project_dir", default=".", help="Project directory")
@click.option("-d", "--detach", is_flag=True,
              help="Start containers in the background and exit (no shell).")
@click.option("--debug", is_flag=True, help="Enable verbose debug logging in containers")
@_project_aware
def up(project_dir: str, detach: bool, debug: bool) -> None:
    """Start agents in Docker containers.

    From a TTY (interactive use), this brings the stack up detached and
    then opens the interactive shell — single window, slash prompt,
    live feed. Use ``--detach`` to skip the shell. Without a TTY (CI,
    pipes), runs ``docker compose up`` in the foreground as before.
    """
    project = Path(project_dir).resolve()
    try:
        compose_file = _find_compose_file(project)
    except ComposeFileNotFound as e:
        raise click.ClickException(str(e)) from None

    # Build a full env dict so the auth-detection helpers can see what's
    # already exported and skip work when the user has things set.
    env = os.environ.copy()
    if debug:
        env["CODEBAND_DEBUG"] = "1"
    _detect_github_auth(env)
    _detect_git_credentials(env)
    _detect_codex_auth(env)

    # Profile-gated pools: derive COMPOSE_PROFILES from codeband.yaml so
    # flipping pool counts in config alone is enough to switch frameworks.
    # "Config wins" — pre-existing planner / plan-reviewer values in the
    # env are stripped and replaced; unrelated user profiles are preserved.
    for config_attr, profile_prefix in _PROFILE_GATED_POOLS:
        _apply_pool_profile(env, project, config_attr, profile_prefix)

    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not detach

    if interactive:
        # Print a codeband-styled banner before docker compose's chatty
        # output so the experience bookends similarly to `cb` (local mode).
        # The auto-attached shell prints its own "docker mode" banner once
        # compose returns successfully.
        from codeband import __version__
        from codeband.shell.render import println, section
        section(f"Codeband v{__version__} — starting Docker stack")
        println("Building images and starting containers… (first run may take ~1 minute)")
        println("")

    from codeband.orchestration.compose import compose_run
    args = ["up", "--build"]
    if detach or interactive:
        args.append("-d")
    result = compose_run(
        project, compose_file, args,
        capture=False,    # stream container build/run output
        check=False,
        timeout=None,
        env=env,
    )

    if interactive and result.returncode == 0:
        # Stack is up — drop into the shell as a thin client. Force
        # attach mode via env var so the shell does NOT also try to
        # start an in-process fleet (would happen if the user's
        # workspace.mode is 'local', the default).
        # CODEBAND_PROJECT_DIR is also exported so the attached shell's
        # later docker compose ps/exec subprocesses pick up the right
        # project context if compose interpolation needs it.
        os.environ["CODEBAND_SHELL_ATTACH"] = "1"
        os.environ["CODEBAND_PROJECT_DIR"] = str(project)
        try:
            os.execvp(sys.argv[0], [sys.argv[0], "--dir", str(project)])
        except OSError as e:
            click.echo(
                f"Stack is running, but the shell couldn't auto-attach "
                f"({type(e).__name__}: {e}).\n"
                f"Open the shell manually with:\n"
                f"  cb --attach --dir {project}",
                err=True,
            )


@cli.command()
@click.option("--dir", "project_dir", default=".", help="Project directory")
@click.option("-v", "--volumes", is_flag=True, help="Also remove volumes")
@_project_aware
def down(project_dir: str, volumes: bool) -> None:
    """Stop Docker containers."""
    project = Path(project_dir).resolve()
    try:
        compose_file = _find_compose_file(project)
    except ComposeFileNotFound as e:
        raise click.ClickException(str(e)) from None

    from codeband.orchestration.compose import compose_run

    env = os.environ.copy()
    for config_attr, profile_prefix in _PROFILE_GATED_POOLS:
        _apply_pool_profile(env, project, config_attr, profile_prefix)

    args = ["down", "--remove-orphans"]
    if volumes:
        args.append("-v")
    compose_run(
        project, compose_file, args,
        capture=False, check=False, timeout=None, env=env,
    )


@cli.command()
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def reset(project_dir: str) -> None:
    """Clean up the active Band.ai task room.

    Removes every agent from the room recorded in .codeband_room and deletes
    the pointer file. Use this when the previous session left stale room
    membership causing 404 warnings on startup.
    """
    project = Path(project_dir).resolve()
    config = load_config(project)

    from codeband.orchestration.kickoff import reset_active_room

    room_id = _run_async(reset_active_room(config, project))
    if room_id is None:
        click.echo("No active task room to reset.")
    else:
        click.echo(f"Reset task room: {room_id}")


@cli.command()
@click.option("--agent", default=None, help="Filter by agent name")
@click.option("--type", "event_type", default=None, help="Filter by message type (comma-separated)")
@click.option("--no-thoughts", is_flag=True, help="Hide agent thinking")
@click.option("--verbose", is_flag=True, help="Show full event content")
@click.option("--history", "-H", is_flag=True,
              help="Replay existing room history before streaming new activity")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def feed(agent: str | None, event_type: str | None, no_thoughts: bool,
         verbose: bool, history: bool, project_dir: str) -> None:
    """Live stream of agent activity from Band.ai."""
    import os

    project = Path(project_dir).resolve()
    config = load_config(project)

    api_key = os.environ.get("BAND_API_KEY")
    if not api_key:
        click.echo("Error: BAND_API_KEY not set. Set it in .env or environment.", err=True)
        sys.exit(1)

    from codeband.config import load_agent_config
    from codeband.monitoring.activity_log import parse_type_filter
    from codeband.monitoring.feed import FeedFormatter, LiveFeed

    agent_config = load_agent_config(project)
    agent_names = {v.agent_id: k for k, v in agent_config.agents.items()}
    type_filter = parse_type_filter(event_type)

    formatter = FeedFormatter(
        agent_names,
        show_thoughts=not no_thoughts,
        agent_filter=agent,
        type_filter=type_filter,
        verbose=verbose,
    )

    from thenvoi.client.rest import AsyncRestClient

    rest = AsyncRestClient(api_key=api_key, base_url=config.band.rest_url)

    # Banner to stderr so an empty stream is distinguishable from a dead feed,
    # and so piped/redirected stdout stays clean.
    if history:
        click.echo(
            "● Live feed — replaying history, then streaming new activity. Ctrl-C to stop.",
            err=True,
        )
    else:
        click.echo(
            "● Live feed — watching for new activity (live only). "
            "Run 'cb log' for history. Ctrl-C to stop.",
            err=True,
        )

    live_feed = LiveFeed(rest, formatter, show_history=history)
    _run_async(live_feed.run())


@cli.command()
@click.option("--agent", default=None, help="Filter by agent name")
@click.option("--since", default=None, help="Show usage since (e.g. 1h, 30m, 2d)")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def usage(agent: str | None, since: str | None, project_dir: str) -> None:
    """Show token usage and cost summary."""
    from codeband.monitoring.usage import UsageSummary
    from codeband.shell.fs import make_backend

    project = Path(project_dir).resolve()
    config = load_config(project)

    reader = make_backend(config, project).make_activity_reader()

    since_dt = _parse_since(since) if since else None
    summary = UsageSummary.from_activity_reader(reader, agent=agent, since=since_dt)

    if summary.call_count == 0:
        click.echo("No LLM usage recorded yet.")
        return

    click.echo()
    click.echo("=" * 56)
    click.echo("  CODEBAND USAGE REPORT")
    click.echo("=" * 56)
    click.echo(f"  Total cost:          ${summary.total_cost_usd:.4f}")
    if summary.total_input_tokens or summary.total_output_tokens:
        click.echo(f"  Total input tokens:  {summary.total_input_tokens:,}")
        click.echo(f"  Total output tokens: {summary.total_output_tokens:,}")
    click.echo(f"  LLM calls:           {summary.call_count}")
    if summary.by_agent:
        click.echo()
        click.echo("  Per agent:")
        for name, agent_summary in sorted(summary.by_agent.items()):
            tokens = ""
            if agent_summary.total_input_tokens or agent_summary.total_output_tokens:
                tokens = (
                    f"  ({agent_summary.total_input_tokens:,} in"
                    f" / {agent_summary.total_output_tokens:,} out)"
                )
            click.echo(
                f"    {name:<20s} ${agent_summary.total_cost_usd:.4f}"
                f"  ({agent_summary.call_count} calls){tokens}"
            )
    click.echo("=" * 56)
    click.echo()


@cli.command()
@click.option("--agent", default=None, help="Filter by agent name")
@click.option("--type", "event_type", default=None,
              help="Filter by event type(s), comma-separated (e.g. NUDGE,ERROR)")
@click.option("--since", default=None, help="Show events since (e.g. 1h, 30m)")
@click.option("--all", "show_all", is_flag=True, help="Include LLM_USAGE events (hidden by default)")
@click.option("--dir", "project_dir", default=".", help="Project directory")
@_project_aware
def log(agent: str | None, event_type: str | None, since: str | None,
        show_all: bool, project_dir: str) -> None:
    """View persistent activity history.

    By default hides LLM_USAGE noise. Use --all or --type LLM_USAGE to see it.
    """
    from codeband.monitoring.activity_log import EventType, parse_type_filter
    from codeband.shell.fs import make_backend

    project = Path(project_dir).resolve()
    config = load_config(project)

    reader = make_backend(config, project).make_activity_reader()

    since_dt = _parse_since(since) if since else None
    type_filter = parse_type_filter(event_type)
    events = reader.read(agent=agent, since=since_dt)

    if type_filter is not None:
        events = [e for e in events if e.event_type in type_filter]
    elif not show_all:
        events = [e for e in events if e.event_type != EventType.LLM_USAGE]

    if not events:
        hint = "" if type_filter is not None else " (use --all to include LLM usage)"
        click.echo(f"No activity events found{hint}.")
        return

    for event in events:
        ts = event.timestamp[11:19]  # HH:MM:SS
        date = event.timestamp[:10]
        agent_name = event.agent
        summary = event.summary

        if event.event_type == EventType.LLM_USAGE and event.details:
            cost = event.details.get("cost_usd", 0)
            source = event.details.get("source", "")
            duration = event.details.get("duration_ms")
            dur_str = f" {duration / 1000:.1f}s" if duration else ""
            summary = f"${cost:.4f}{dur_str} ({source})"

        click.echo(f"{date} {ts}  {event.event_type:<18s} {agent_name:<16s} {summary}")


def _parse_since(value: str):
    """Parse a --since value like '1h', '30m', '2d', or an ISO date.

    Raises ``click.BadParameter`` on malformed input so the CLI reports a
    clean error instead of an uncaught ValueError traceback.
    """
    from datetime import UTC, datetime as dt, timedelta

    value = value.strip()
    units = {"h": "hours", "m": "minutes", "d": "days"}
    unit = value[-1:] if value else ""
    if unit in units:
        try:
            amount = float(value[:-1])
        except ValueError:
            raise click.BadParameter(
                f"{value!r} — expected a number before '{unit}', e.g. 1h, 30m, 2d."
            ) from None
        return dt.now(UTC) - timedelta(**{units[unit]: amount})
    try:
        return dt.fromisoformat(value)
    except ValueError:
        raise click.BadParameter(
            f"{value!r} — use a relative span (1h, 30m, 2d) or an ISO date "
            "(YYYY-MM-DD)."
        ) from None




def _detect_git_credentials(env: dict[str, str]) -> None:
    """Detect host git credentials and set env vars for Docker containers."""
    home = Path.home()

    # Check for ~/.git-credentials (git credential store)
    git_creds = home / ".git-credentials"
    if git_creds.is_file():
        env.setdefault("GIT_CREDENTIALS_PATH", str(git_creds))
        return

    # Check for SSH key
    for key_name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        ssh_key = home / ".ssh" / key_name
        if ssh_key.is_file():
            env.setdefault("SSH_AUTH_DIR", str(home / ".ssh"))
            return


# Pools whose two framework variants are profile-gated in the bundled
# compose files. Each entry is (config_attr, profile_prefix); the gate
# names are {claude,codex}-{prefix}.
_PROFILE_GATED_POOLS: tuple[tuple[str, str], ...] = (
    ("planners", "planner"),
    ("plan_reviewers", "plan-reviewer"),
)


def _pool_profile_names(profile_prefix: str) -> tuple[str, str]:
    """Return the two profile names that gate a pool's compose services."""
    return (f"claude-{profile_prefix}", f"codex-{profile_prefix}")


def _pool_compose_profiles(
    project_dir: Path, config_attr: str, profile_prefix: str,
) -> list[str]:
    """Resolve compose profiles for a profile-gated pool from codeband.yaml.

    Reads ``agents.<config_attr>.{claude_sdk,codex}.count`` and returns
    the matching profile names. Returns an empty list when the config is
    missing/unreadable — safer to start nothing than the wrong service.
    """
    try:
        config = load_config(project_dir)
    except Exception as exc:  # noqa: BLE001
        click.echo(
            f"Warning: could not read codeband.yaml for {profile_prefix} profile "
            f"selection ({type(exc).__name__}: {exc}); no {profile_prefix} profile "
            "will be activated.",
            err=True,
        )
        return []

    pool = getattr(config.agents, config_attr)
    claude_profile, codex_profile = _pool_profile_names(profile_prefix)
    profiles: list[str] = []
    if pool.claude_sdk.count > 0:
        profiles.append(claude_profile)
    if pool.codex.count > 0:
        profiles.append(codex_profile)
    return profiles


def _apply_pool_profile(
    env: dict[str, str],
    project_dir: Path,
    config_attr: str,
    profile_prefix: str,
) -> None:
    """Sync ``env["COMPOSE_PROFILES"]`` with codeband.yaml for one pool.

    Always strips both pool-related profile values (``claude-<prefix>`` and
    ``codex-<prefix>``) from the existing list, then appends whatever the
    config derives. "Config wins" is absolute: if the config has count: 0
    on both frameworks, a stale ``COMPOSE_PROFILES=claude-<prefix>`` from
    the shell is also stripped — Docker won't run a service the config
    didn't ask for. Unrelated user profiles (``debug``, ``monitoring``,
    other pool gates) are preserved.
    """
    derived = _pool_compose_profiles(project_dir, config_attr, profile_prefix)
    pool_profiles = _pool_profile_names(profile_prefix)
    raw = env.get("COMPOSE_PROFILES", "").split(",")
    existing = [p for p in raw if p and p not in pool_profiles]
    combined = [*existing, *derived]
    if combined:
        env["COMPOSE_PROFILES"] = ",".join(combined)
    elif "COMPOSE_PROFILES" in env:
        # Don't leave behind an empty string — drop the key entirely so
        # docker compose doesn't see a malformed profile list.
        del env["COMPOSE_PROFILES"]


def _detect_codex_auth(env: dict[str, str]) -> None:
    """Ensure host ~/.codex exists and export it for the docker-compose mount.

    Codex stores subscription credentials (from ``codex login --device-auth``)
    and refreshes OAuth tokens in ``~/.codex/auth.json``. To let containers
    use those credentials we bind-mount the host directory; the directory
    must exist on the host or Docker fails the mount.

    A user without Codex configured gets an empty directory, which is
    harmless — the entrypoint falls back to API-key auth in that case.
    """
    if env.get("CODEX_HOME"):
        return
    codex_home = Path.home() / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    env["CODEX_HOME"] = str(codex_home)


def _detect_github_auth(env: dict[str, str]) -> None:
    """Detect GitHub auth for gh CLI and export it to Docker containers."""
    if env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"):
        return

    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return

    if result.returncode != 0:
        return

    token = result.stdout.strip()
    if token:
        env.setdefault("GH_TOKEN", token)
