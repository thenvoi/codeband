# Contributing to Codeband

Thanks for taking the time to improve Codeband. This project is early-stage, so the best contributions are focused, easy to review, and tied to a concrete issue or user workflow.

## Good First Contributions

Good starter areas:

- Documentation fixes, setup clarifications, and examples
- `cb doctor` checks and remediation hints
- Small CLI usability improvements
- Focused tests around existing behavior
- Bug fixes with a clear reproduction

For larger behavior changes, open an issue first so we can agree on the shape before you spend time implementing it.

## Development Setup

```bash
git clone https://github.com/band-ai/codeband.git
cd codeband
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the checks before opening a pull request:

```bash
pytest
ruff check src/ tests/
```

## Pull Request Checklist

- Include tests for behavior changes.
- Keep unrelated refactors out of the PR.
- Update docs when user-visible behavior changes.
- Run `pytest` and `ruff check src/ tests/`.
- Describe the problem, the fix, and any remaining tradeoffs.

## Issue Reports

When reporting a bug, include:

- Operating system and Python version
- `codeband` version
- The command you ran
- Relevant `cb doctor` output
- Whether you are using local, Docker, or distributed mode
- Any redacted logs or tracebacks

Do not include API keys, OAuth tokens, GitHub tokens, or Band.ai agent credentials in issues.
