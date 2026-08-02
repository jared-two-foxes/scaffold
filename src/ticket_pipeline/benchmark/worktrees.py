"""
Git-worktree management helpers for isolated benchmark trial execution.

Each trial runs in a dedicated worktree so that concurrent trials never
share a working directory or clobber each other's state files.

The ``_WORKTREE_LOCK`` serialises ``git worktree add/remove`` calls because
Git's own bookkeeping is not safe for parallel invocations on the same repo.
The worktrees themselves run fully in parallel once created.
"""

from __future__ import annotations

import subprocess
import threading
import uuid
from pathlib import Path

_WORKTREE_LOCK = threading.Lock()


def create_worktree(repo: Path, base_ref: str, worktrees_base: Path) -> Path:
    """
    Create a detached git worktree at ``worktrees_base/<random-id>`` checked
    out at *base_ref*.

    Returns the path to the newly created worktree.
    Raises :class:`subprocess.CalledProcessError` on failure.
    """
    wt_path = worktrees_base / uuid.uuid4().hex[:12]
    worktrees_base.mkdir(parents=True, exist_ok=True)
    with _WORKTREE_LOCK:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "--detach",
                str(wt_path),
                base_ref,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return wt_path


def remove_worktree(repo: Path, wt_path: Path) -> None:
    """
    Remove the worktree at *wt_path*.  Never raises; failures are silently
    ignored so cleanup does not mask trial errors.
    """
    with _WORKTREE_LOCK:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt_path)],
            check=False,
            capture_output=True,
            text=True,
        )
