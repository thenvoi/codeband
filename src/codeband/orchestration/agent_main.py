"""Per-agent entry point for distributed deployment.

Reads AGENT_KEY from the environment and runs that single agent.
Used by Docker containers and ``codeband run --agent <key>``.

Usage::

    AGENT_KEY=conductor           python -m codeband.orchestration.agent_main
    AGENT_KEY=coder-claude_sdk-0  python -m codeband.orchestration.agent_main
    AGENT_KEY=reviewer-codex-0    python -m codeband.orchestration.agent_main
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def main() -> None:
    """Entry point: resolve config and run the specified agent."""
    debug = os.environ.get("CODEBAND_DEBUG", "").lower() in ("1", "true", "yes")
    log_level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s")

    from codeband.logging_setup import install_session_resume_filter
    install_session_resume_filter()

    agent_key = os.environ.get("AGENT_KEY")
    if not agent_key:
        print("Error: AGENT_KEY environment variable is required.", file=sys.stderr)
        sys.exit(1)

    config_path = Path(os.environ.get("CODEBAND_CONFIG", "codeband.yaml")).resolve()
    project_dir = config_path.parent

    from codeband.config import CodebandConfig
    config = CodebandConfig.from_yaml(config_path)

    # Distributed-mode rehydration (RFC WS5) is wired inside ``run_agent``: it
    # resolves the workspace path, opens the durable StateStore, and prepends
    # per-role recovery context to the agent's system prompt before
    # ``agent.run()``. Kept there (not here) because ``main()`` only has the raw
    # config + project_dir, while ``run_agent`` already resolves the workspace.
    from codeband.orchestration.runner import run_agent
    asyncio.run(run_agent(config, project_dir, agent_key))


if __name__ == "__main__":
    main()
