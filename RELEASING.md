# Releasing Codeband

Codeband is published to PyPI as [`codeband`](https://pypi.org/project/codeband/).
Releases are cut by pushing a `vX.Y.Z` tag; GitHub Actions builds the sdist and
wheel and uploads them to PyPI via [PyPI Trusted Publishing][tp] (OIDC, no API
tokens).

[tp]: https://docs.pypi.org/trusted-publishers/

## One-time PyPI setup

Before the first release, register the GitHub repo as a trusted publisher on
PyPI:

1. Sign in to https://pypi.org and go to **Your projects** → **codeband** →
   **Publishing** (or, for the very first release, **Add a pending publisher**
   from your account page).
2. Add a publisher with:
   - Owner: the GitHub org/user that owns the repo
   - Repository: `codeband`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. In the GitHub repo, create a **`pypi`** environment
   (Settings → Environments → New environment). Optionally restrict it to
   protected tags so only maintainers can trigger publishes.

TestPyPI works the same way — register a separate trusted publisher there if
you want a staging release channel.

## Cutting a release

1. Bump the version in two places (they must match — CI enforces this):
   - `pyproject.toml` → `[project] version`
   - `src/codeband/__init__.py` → `__version__`
2. Update `CHANGELOG.md`: rename the `Unreleased` section to the new version
   and add a fresh `Unreleased` heading on top.
3. Commit and push to `main`.
4. Tag and push:
   ```bash
   git tag -a v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```
5. The `Publish to PyPI` workflow runs, verifies the tag matches the package
   version, builds, and uploads. Watch it under the **Actions** tab.
6. Verify:
   ```bash
   pip install --upgrade codeband
   codeband --version  # or: cb --version
   ```

## Building locally (smoke test)

```bash
pip install --upgrade build twine
python -m build
twine check dist/*
```

The repo is configured as a `src/` layout with `setuptools.packages.find`, so
`python -m build` produces both an sdist and a wheel under `dist/`. Prompt
files are bundled via `[tool.setuptools.package-data]`.

## Manual fallback

If trusted publishing is ever unavailable, you can publish from a maintainer
machine:

```bash
python -m build
twine upload dist/*   # uses ~/.pypirc or TWINE_USERNAME/TWINE_PASSWORD
```

Prefer the tag-driven workflow — it leaves an auditable trail and avoids any
local credential handling.
