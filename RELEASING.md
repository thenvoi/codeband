# Releasing Codeband

Codeband is published to PyPI as [`codeband`](https://pypi.org/project/codeband/)
under the [Band PyPI org](https://pypi.org/org/Band/). Releases are cut by
pushing a `vX.Y.Z` tag; GitHub Actions builds the sdist and wheel and uploads
them to PyPI via [Trusted Publishing][tp] (OIDC, no API tokens stored).

[tp]: https://docs.pypi.org/trusted-publishers/

## One-time setup (PyPI side)

Done once by a maintainer with admin access to the `Band` PyPI org.

1. Sign in to https://pypi.org as a member of the **Band** org.
2. Go to **Your account** → **Publishing** → **Add a pending publisher** (use a
   pending publisher because the `codeband` project does not exist on PyPI yet
   — the first successful publish creates it under the org).
3. Fill in:
   - **PyPI Project Name**: `codeband`
   - **Owner**: `thenvoi`
   - **Repository name**: `codeband`
   - **Workflow name**: `publish.yml`
   - **Environment name**: `release`

After the first release, the project will appear under
https://pypi.org/manage/project/codeband/. Future trusted-publisher edits live
under the project's **Publishing** tab.

## One-time setup (GitHub side)

Done once on the `thenvoi/codeband` repo.

1. Settings → Environments → **New environment** → name it `release`.
2. (Optional, recommended) Restrict the environment to protected tags matching
   `v*` so only maintainers can trigger a publish.

## Cutting a release

1. Bump the version in two places — the publish workflow fails the build if
   they drift:
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
5. Watch the **Publish to PyPI** workflow under **Actions**. It verifies the
   tag against both version files, builds, and uploads. Total runtime ~2
   minutes.
6. Verify in a clean venv:
   ```bash
   pip install --upgrade codeband
   cb --version
   ```

## Building locally (smoke test)

```bash
pip install --upgrade build twine
python -m build
twine check dist/*
```

The repo uses a `src/` layout with `setuptools.packages.find`, so `python -m
build` produces both an sdist and a wheel under `dist/`. Prompt files are
bundled via `[tool.setuptools.package-data]`.

## Failure modes

- **Tag/version drift** — the build job's verify step fails before any upload.
  Fix: delete the bad tag (`git tag -d vX.Y.Z && git push origin
  :refs/tags/vX.Y.Z`), align the versions, re-tag.
- **OIDC rejected by PyPI** — confirm the Trusted Publisher entry matches
  exactly: owner `thenvoi`, repo `codeband`, workflow `publish.yml`,
  environment `release`. Re-run the failed job from the Actions tab.
- **Bad release shipped** — you can `yank` a release on PyPI (hides from
  default `pip install`, keeps it for pinned installs). You cannot delete a
  version once published. Ship the next patch version with the fix.
- **Re-publishing the same version** — not allowed by PyPI. Bump and re-tag.

## Manual fallback

If Trusted Publishing is ever unavailable, a maintainer with `Band` org upload
permissions can publish from a local machine:

```bash
python -m build
twine upload dist/*   # uses ~/.pypirc or TWINE_USERNAME/TWINE_PASSWORD
```

Prefer the tag-driven workflow — it leaves an auditable trail in GitHub
Actions and avoids any local credential handling.
