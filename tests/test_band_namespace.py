"""Guards the migration off the dead ``thenvoi`` SDK namespace.

band-sdk 1.0.0 renamed the SDK module namespace ``thenvoi`` -> ``band`` and
builds its MCP tool names as ``band_<verb>`` (band/runtime/tools.py:
``prefixed_name = f"band_{name}"``). There is no ``thenvoi_*`` tool alias and no
``thenvoi`` top-level package anymore, so any leftover reference is a runtime
break: imports raise ``ModuleNotFoundError: No module named 'thenvoi'`` and
prompts that name ``thenvoi_send_message`` tell agents to call tools that do
not exist.

The separate REST client package ``thenvoi_rest`` (imported as
``from thenvoi_rest import ...``) is unrelated and still installed, so these
guards must not flag it.
"""

from __future__ import annotations

import re
from pathlib import Path

import codeband

_PKG = Path(codeband.__file__).parent

# Any ``thenvoi`` reference except the two still-installed sibling packages
# ``thenvoi_rest`` (REST client) and ``thenvoi_testing``. Catches the dead SDK
# namespace (``thenvoi.adapters``), the bare module (``from thenvoi import``),
# and stale ``thenvoi_<verb>`` tool names left in code comments.
_DEAD_NAMESPACE = re.compile(r"\bthenvoi(?!_rest\b|_testing\b)")
# Dead MCP tool-name prefix used in prompts.
_DEAD_TOOL_PREFIX = re.compile(r"\bthenvoi_(?!rest\b|testing\b)")


def test_no_python_source_imports_dead_thenvoi_namespace():
    offenders: list[str] = []
    for path in _PKG.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DEAD_NAMESPACE.search(line):
                offenders.append(f"{path.relative_to(_PKG)}:{lineno}: {line.strip()}")
    assert not offenders, "Dead 'thenvoi' SDK namespace still referenced:\n" + "\n".join(
        offenders
    )


def test_no_prompt_references_dead_thenvoi_tool_prefix():
    offenders: list[str] = []
    for path in (_PKG / "prompts").rglob("*.md"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DEAD_TOOL_PREFIX.search(line):
                offenders.append(f"{path.relative_to(_PKG)}:{lineno}: {line.strip()}")
    assert not offenders, "Dead 'thenvoi_' MCP tool prefix still in prompts:\n" + "\n".join(
        offenders
    )
