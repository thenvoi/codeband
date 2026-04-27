"""Codeband interactive shell — single-terminal slash-command REPL.

Bare ``cb`` (no subcommand, TTY) opens this shell. In local mode the shell
runs the orchestrator in-process alongside the prompt + live feed; in
distributed mode it acts as a thin client that talks to containerized
agents via Band.ai (API-bound slash commands) and ``docker compose exec``
(filesystem-bound slash commands).
"""

from __future__ import annotations
