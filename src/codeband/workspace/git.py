"""Git workspace management — bare clones and worktrees."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """Raised when workspace operations fail."""


def clone_bare(repo_url: str, dest: Path) -> None:
    """Clone a repository as bare (shared object store).

    After cloning, configures the standard fetch refspec so that
    ``git fetch origin`` creates remote tracking branches (``origin/main``
    etc.) instead of mapping directly to local branch heads — which is the
    default ``git clone --bare`` behavior.
    """
    if dest.exists():
        logger.info("Bare repo already exists at %s", dest)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Cloning bare repo: %s -> %s", repo_url, dest)
    _run_git(["clone", "--bare", repo_url, str(dest)])
    # bare clones lack a fetch refspec — add one so origin/* refs exist
    _run_git(
        ["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
        cwd=dest,
    )
    _run_git(["fetch", "origin"], cwd=dest)


def pin_gh_default_repo(worktree: Path, repo_url: str) -> None:
    """Pin gh's default repo for `worktree` to the slug derived from `repo_url`.

    Hard guarantee: a Coder running `gh pr create` from this worktree without
    flags will target the configured repo, not the upstream parent. Without
    this pinning, gh defaults base-repo resolution to the parent fork, which
    silently opens PRs against the upstream public repo (the bug that hit
    PR #1469 against Delgan/loguru). `gh repo set-default` writes the pinned
    slug to `.git/config` under `[remote "origin"] gh-resolved`.

    Idempotent — safe to call repeatedly. No-ops when:
      - `gh` is not installed (Coder will see a clear error at PR time).
      - The URL is not a recognizable GitHub URL (gh only knows GitHub).
      - The set-default call returns non-zero — logged at WARN, not raised,
        so a missing gh-config does not block workspace setup.
    """
    if shutil.which("gh") is None:
        logger.debug("gh CLI not found; skipping repo set-default for %s", worktree)
        return
    try:
        from codeband.github.prs import repo_slug
        slug = repo_slug(repo_url)
    except ValueError:
        logger.debug("repo URL %r is not GitHub; skipping gh set-default", repo_url)
        return
    try:
        subprocess.run(
            ["gh", "repo", "set-default", slug],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        logger.info("Pinned gh default repo for %s to %s", worktree, slug)
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "gh repo set-default failed in %s (slug=%s): %s",
            worktree, slug, (exc.stderr or "").strip(),
        )
    except subprocess.TimeoutExpired:
        logger.warning("gh repo set-default timed out in %s", worktree)


def create_worktree(
    bare_repo: Path,
    worktree_path: Path,
    branch: str,
    *,
    detach: bool = False,
    base_branch: str | None = None,
) -> None:
    """Create a git worktree from the bare repo.

    If detach=True, creates a detached HEAD worktree at the branch tip.
    If base_branch is provided, new branches are created from origin/<base_branch>
    instead of HEAD (ensures the correct starting point).
    """
    if worktree_path.exists():
        if _is_valid_worktree(worktree_path):
            logger.info("Worktree already exists at %s", worktree_path)
            return
        logger.warning(
            "Directory exists but is not a valid worktree, recreating: %s",
            worktree_path,
        )

    # Always prune stale worktree records before creating — handles both
    # "directory exists but invalid" and "directory deleted externally but
    # git still tracks the worktree path".
    _run_git(["worktree", "prune"], cwd=bare_repo)
    if worktree_path.exists():
        shutil.rmtree(worktree_path)

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Create branch if it doesn't exist, then add worktree
    existing_branches = _run_git(
        ["branch", "--list", branch], cwd=bare_repo
    ).strip()

    if detach:
        logger.info("Creating detached worktree at %s (branch: %s)", worktree_path, branch)
        _run_git(
            ["worktree", "add", "--detach", str(worktree_path), branch],
            cwd=bare_repo,
        )
    elif existing_branches:
        logger.info("Creating worktree on existing branch: %s", branch)
        _run_git(
            ["worktree", "add", str(worktree_path), branch],
            cwd=bare_repo,
        )
    else:
        logger.info("Creating worktree with new branch: %s", branch)
        cmd = ["worktree", "add", "-b", branch, str(worktree_path)]
        if base_branch:
            cmd.append(base_branch)
        _run_git(cmd, cwd=bare_repo)


def refresh_worktree(
    bare_repo: Path,
    worktree_path: Path,
    branch: str,
    *,
    detach: bool = False,
) -> None:
    """Fast-forward an existing worktree to ``origin/<branch>`` at session start.

    ``create_worktree`` deliberately no-ops on an existing worktree, and the
    bare repo only fetches at clone time — so across sessions every reused
    planner / plan-reviewer / mergemaster worktree silently aged (finding 16:
    planning and integration-testing against a stale base). This is the
    session-start refresh:

    * ``git fetch origin`` in the bare repo first. A fetch *failure* (network
      down) is a WARNING and the refresh proceeds on cached refs — infra
      flakiness must not block startup, matching ``_recreate_worktree``.
    * A dirty worktree **fails loud** (:class:`WorkspaceError` naming the
      paths): local state that prevents the fast-forward means the session
      would otherwise silently plan against a stale base.
    * ``detach=True`` (planner / plan-reviewer, read-only): re-detach at the
      refreshed ref. Otherwise (mergemaster): ``merge --ff-only`` — a
      diverged local branch fails loud the same way.

    Falls back from ``origin/<branch>`` to ``<branch>`` when no remote ref
    exists (offline/bare-only test repos), mirroring ``prepare_task_branch``.
    Coder worktrees are deliberately NOT refreshed here: their workspace
    branches are reset by ``prepare_task_branch`` at task assignment, and a
    session-start reset would clobber the crash-recovery state the supervisor
    rebuilds from uncommitted changes.
    """
    try:
        _run_git(["fetch", "origin"], cwd=bare_repo)
    except WorkspaceError as exc:
        logger.warning(
            "fetch failed during session-start refresh of %s (%s); "
            "refreshing from cached refs", worktree_path, exc,
        )

    status = _run_git(["status", "--porcelain"], cwd=worktree_path).strip()
    if status:
        raise WorkspaceError(
            f"Worktree {worktree_path} has local changes that prevent the "
            f"session-start fast-forward to origin/{branch}:\n{status}\n"
            "Refusing to start on a stale base — clean or remove the "
            "worktree, then restart."
        )

    try:
        _run_git(["rev-parse", "--verify", f"origin/{branch}"], cwd=worktree_path)
        ref = f"origin/{branch}"
    except WorkspaceError:
        ref = branch

    if detach:
        _run_git(["checkout", "--detach", ref], cwd=worktree_path)
        return
    _run_git(["checkout", branch], cwd=worktree_path)
    try:
        _run_git(["merge", "--ff-only", ref], cwd=worktree_path)
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"Worktree {worktree_path} branch {branch!r} cannot fast-forward "
            f"to {ref} (diverged?). Refusing to start on a stale base — "
            f"reset or remove the worktree, then restart. {exc}"
        ) from None


def branch_exists(bare_repo: Path, branch: str) -> bool:
    """True if ``branch`` resolves to a ref (local head or remote-tracking)."""
    try:
        subprocess.run(
            ["git", "-C", str(bare_repo), "rev-parse", "--verify", "--quiet", branch],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def list_known_branches(bare_repo: Path) -> list[str]:
    """Return all local + remote-tracking branches, deduplicated and sorted.

    Strips the ``origin/`` prefix so the names match what the user would
    write in ``codeband.yaml``. Skips ``HEAD`` symrefs.
    """
    output = _run_git(
        ["branch", "-a", "--format=%(refname:short)"], cwd=bare_repo
    )
    names: set[str] = set()
    for line in output.splitlines():
        name = line.strip()
        if not name or name == "HEAD":
            continue
        if name.startswith("origin/"):
            stripped = name[len("origin/"):]
            if stripped == "HEAD":
                continue
            names.add(stripped)
        else:
            names.add(name)
    return sorted(names)


def remove_worktree(bare_repo: Path, worktree_path: Path) -> None:
    """Remove a git worktree."""
    if not worktree_path.exists():
        return

    logger.info("Removing worktree: %s", worktree_path)
    _run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=bare_repo)


def list_worktrees(bare_repo: Path) -> list[str]:
    """List all worktree paths for the bare repo."""
    output = _run_git(["worktree", "list", "--porcelain"], cwd=bare_repo)
    paths = []
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.append(line[len("worktree "):])
    return paths


def branch_name(prefix: str, worker_id: str, task_slug: str) -> str:
    """Generate a branch name: codeband/coder-claude_sdk-0/implement-auth."""
    safe_slug = task_slug.lower().replace(" ", "-")[:50]
    return f"{prefix}/{worker_id}/{safe_slug}"


def prepare_task_branch(
    worktree_path: Path,
    task_branch: str,
    base_branch: str = "main",
    *,
    resume: bool = False,
) -> None:
    """Prepare a coder worktree for a task.

    If resume=True, checks out the existing task branch (crash recovery).
    Otherwise, fetches latest, resets to base branch, and creates the task branch.

    If the worktree is in a broken state (e.g. missing refs after a failed
    previous run), it is automatically recreated from the bare repo.
    """
    try:
        _prepare_task_branch_inner(
            worktree_path, task_branch, base_branch, resume=resume,
        )
    except WorkspaceError:
        logger.warning(
            "Worktree %s is in a broken state, recreating from bare repo",
            worktree_path,
        )
        _recreate_worktree(worktree_path, base_branch)
        _prepare_task_branch_inner(
            worktree_path, task_branch, base_branch, resume=False,
        )


def _recreate_worktree(worktree_path: Path, base_branch: str) -> None:
    """Remove and recreate a worktree from the bare repo.

    Order of operations is verify-before-delete: fetch (best effort) and
    confirm the base ref exists in the bare repo *before* removing the
    worktree. If neither ``origin/<base>`` nor ``<base>`` resolves, raise
    without touching the existing directory — leaving a broken worktree is
    strictly better than leaving a missing one, since the next cycle can
    retry once the network is back.
    """
    gitdir_file = worktree_path / ".git"
    if not gitdir_file.is_file():
        raise WorkspaceError(f"Cannot find .git pointer in {worktree_path}")
    # .git file contains "gitdir: /path/to/.codeband/repo.git/worktrees/<name>"
    gitdir_ref = gitdir_file.read_text().strip()
    worktree_gitdir = Path(gitdir_ref.removeprefix("gitdir: "))
    bare_repo = worktree_gitdir.parent.parent  # repo.git/worktrees/<name> -> repo.git

    # Read the branch this worktree was on before removing it.
    try:
        branch = _run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path,
        ).strip()
    except WorkspaceError:
        branch = worktree_path.name  # fallback: directory name

    # Best-effort fetch. If it fails (network down, host unreachable), we
    # may still be able to recreate from cached refs.
    try:
        _run_git(["fetch", "origin"], cwd=bare_repo)
    except WorkspaceError as exc:
        logger.warning(
            "fetch failed during worktree recreate (%s); "
            "attempting to recreate from cached refs", exc,
        )

    # Verify the base ref BEFORE removing the worktree.
    create_base: str | None = None
    for candidate in (f"origin/{base_branch}", base_branch):
        try:
            _run_git(["rev-parse", "--verify", candidate], cwd=bare_repo)
            create_base = candidate
            break
        except WorkspaceError:
            continue

    if create_base is None:
        raise WorkspaceError(
            f"Cannot recreate worktree {worktree_path}: base ref "
            f"{base_branch!r} not found locally and fetch did not bring it in. "
            f"Existing worktree left intact for retry on the next cycle."
        )

    remove_worktree(bare_repo, worktree_path)
    create_worktree(bare_repo, worktree_path, branch, base_branch=create_base)


def _prepare_task_branch_inner(
    worktree_path: Path,
    task_branch: str,
    base_branch: str = "main",
    *,
    resume: bool = False,
) -> None:
    """Inner implementation of prepare_task_branch (may raise WorkspaceError)."""
    _run_git(["fetch", "origin"], cwd=worktree_path)

    if resume:
        # Check if the task branch exists locally or on remote
        existing = _run_git(
            ["branch", "--list", task_branch], cwd=worktree_path,
        ).strip()
        if existing:
            logger.info("Resuming task branch: %s", task_branch)
            _run_git(["checkout", task_branch], cwd=worktree_path)
            return
        # Try remote
        remote_ref = f"origin/{task_branch}"
        try:
            _run_git(["rev-parse", "--verify", remote_ref], cwd=worktree_path)
            logger.info("Resuming task branch from remote: %s", task_branch)
            _run_git(["checkout", "-b", task_branch, remote_ref], cwd=worktree_path)
            return
        except WorkspaceError:
            logger.warning(
                "Task branch %s not found locally or on remote, creating fresh",
                task_branch,
            )

    # Fresh start: reset to base branch and create task branch.
    # Prefer origin/{base} but fall back to {base} for bare repos without remote refs.
    try:
        _run_git(["rev-parse", "--verify", f"origin/{base_branch}"], cwd=worktree_path)
        reset_ref = f"origin/{base_branch}"
    except WorkspaceError:
        reset_ref = base_branch
    logger.info("Preparing task branch %s from %s", task_branch, reset_ref)

    # A coder worktree can survive a previous interrupted assignment. Reset
    # tracked changes, remove untracked files, detach from any stale branch, and
    # then recreate the requested task branch from the requested base ref.
    _run_git(["reset", "--hard", reset_ref], cwd=worktree_path)
    _run_git(["clean", "-fd"], cwd=worktree_path)
    _run_git(["checkout", "--detach", reset_ref], cwd=worktree_path)
    _run_git(["reset", "--hard", reset_ref], cwd=worktree_path)
    # Delete stale local task branch if it exists
    try:
        _run_git(["branch", "-D", task_branch], cwd=worktree_path)
    except WorkspaceError:
        pass  # Branch doesn't exist locally, fine
    _run_git(["checkout", "-b", task_branch, reset_ref], cwd=worktree_path)


def commit_and_push(
    worktree_path: Path,
    message: str,
    remote: str = "origin",
    branch: str | None = None,
) -> None:
    """Stage all changes, commit, and push."""
    _run_git(["add", "-A"], cwd=worktree_path)

    # Check if there are changes to commit
    status = _run_git(["status", "--porcelain"], cwd=worktree_path).strip()
    if not status:
        logger.info("No changes to commit in %s", worktree_path)
        return

    _run_git(["commit", "-m", message], cwd=worktree_path)

    if branch is None:
        branch = _run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path
        ).strip()

    _run_git(["push", "-u", remote, branch], cwd=worktree_path)


def get_worktree_summary(worktree_path: Path, max_log: int = 20) -> str:
    """Return a human-readable summary of worktree state (log, diff, status)."""
    sections: list[str] = []

    try:
        log = _run_git(
            ["log", "--oneline", "-n", str(max_log)], cwd=worktree_path
        ).strip()
        sections.append(f"### Git History (most recent first)\n{log or 'No commits yet'}")
    except WorkspaceError:
        sections.append("### Git History\nUnable to read git log")

    try:
        diff_stat = _run_git(["diff", "--stat"], cwd=worktree_path).strip()
        sections.append(
            f"### Uncommitted Changes\n{diff_stat or 'None'}"
        )
    except WorkspaceError:
        pass

    try:
        status = _run_git(["status", "--porcelain"], cwd=worktree_path).strip()
        if status:
            sections.append(f"### Untracked/Modified Files\n{status}")
    except WorkspaceError:
        pass

    return "\n\n".join(sections)


def create_integration_branch(
    worktree_path: Path, branch_name: str, base: str = "main",
) -> None:
    """Create a new integration branch from base in the given worktree."""
    _run_git(["checkout", base], cwd=worktree_path)
    _run_git(["checkout", "-b", branch_name], cwd=worktree_path)


def merge_branch(worktree_path: Path, branch: str) -> tuple[bool, str]:
    """Attempt to merge a branch. Returns (success, output).

    On conflict, aborts the merge and returns (False, conflicting_files).
    """
    cmd = ["git", "merge", "--no-ff", branch]
    try:
        result = subprocess.run(
            cmd, cwd=worktree_path, capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True, result.stdout
        # Merge conflict — capture both stdout and stderr for conflict details
        conflict_info = (result.stdout + "\n" + result.stderr).strip()
        try:
            _run_git(["merge", "--abort"], cwd=worktree_path)
        except WorkspaceError:
            pass
        return False, conflict_info
    except subprocess.TimeoutExpired:
        return False, "Merge timed out"


def fast_forward_branch(
    worktree_path: Path, target: str, source: str,
) -> None:
    """Fast-forward target branch to source branch tip."""
    _run_git(["checkout", target], cwd=worktree_path)
    _run_git(["merge", "--ff-only", source], cwd=worktree_path)


def delete_branch(worktree_path: Path, branch: str) -> None:
    """Delete a local branch."""
    _run_git(["branch", "-D", branch], cwd=worktree_path)


def _is_valid_worktree(path: Path) -> bool:
    """Check whether *path* is a functional git worktree."""
    try:
        subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _run_git(args: list[str], cwd: Path | None = None) -> str:
    """Run a git command and return stdout."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise WorkspaceError(
            f"Git command failed: {' '.join(cmd)}\n"
            f"stderr: {e.stderr}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise WorkspaceError(f"Git command timed out: {' '.join(cmd)}") from e
