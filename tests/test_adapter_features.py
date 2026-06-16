"""Claude adapters must declare features via the SDK's ``AdapterFeatures`` API.

band-sdk deprecated the ``enable_execution_reporting`` / ``enable_memory_tools``
constructor flags in favour of ``features=AdapterFeatures(...)``. These tests
pin two things at once:

1. Constructing a Claude runner emits no deprecation warning for those flags.
2. The resulting feature set is unchanged — execution + thoughts reporting and
   the memory capability stay on. This guards against "fixing" the warning by
   simply dropping the flags, which would silently disable both.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from band.core.types import Capability, Emit


def _claude_runner_cases(workspace: str):
    from codeband.agents.code_reviewer import ClaudeCodeReviewerRunner
    from codeband.agents.conductor import ClaudeConductorRunner
    from codeband.agents.mergemaster import ClaudeMergemasterRunner
    from codeband.agents.plan_reviewer import ClaudePlanReviewerRunner
    from codeband.agents.planner import ClaudePlannerRunner
    from codeband.agents.player_claude import ClaudePlayerRunner

    ws = {"workspace": workspace}
    return [
        (ClaudePlannerRunner, ws),
        (ClaudeConductorRunner, {}),
        (ClaudePlayerRunner, ws),
        (ClaudeCodeReviewerRunner, ws),
        (ClaudePlanReviewerRunner, ws),
        (ClaudeMergemasterRunner, ws),
    ]


@pytest.mark.parametrize(
    "runner_cls_index", range(6), ids=lambda i: f"case{i}"
)
def test_claude_runner_uses_features_api_without_deprecation(
    runner_cls_index: int, tmp_path: Path
):
    cls, kwargs = _claude_runner_cases(str(tmp_path))[runner_cls_index]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        runner = cls(**kwargs)

    deprecated = [
        str(w.message)
        for w in caught
        if "enable_execution_reporting" in str(w.message)
        or "enable_memory_tools" in str(w.message)
    ]
    assert not deprecated, f"{cls.__name__} still uses deprecated kwargs: {deprecated}"

    features = runner.adapter.features
    assert set(features.emit) == {Emit.EXECUTION, Emit.THOUGHTS}, cls.__name__
    assert set(features.capabilities) == {Capability.MEMORY}, cls.__name__
