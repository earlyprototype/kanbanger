"""Exercise concurrent post-commit hooks with a file-backed fake Project."""

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import kanban_io
import sync_kanban
from tests.test_pre_commit_hook_tty import _resolve_bash


ROOT = Path(__file__).resolve().parents[1]


def wait_for(path, worker=None):
    deadline = time.monotonic() + 30
    while not path.exists():
        if worker is not None and worker.done():
            worker.result()
            raise AssertionError(f"Hook exited before creating {path.name}")
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {path.name}")
        time.sleep(0.02)


class FakeProject:
    def __init__(self, token):
        self.hook = os.environ["TEST_HOOK_NAME"]

    def get_repo_project(self, owner, repo, project_number):
        return "repo", "project", "status", {"Todo": "todo-option"}

    def create_draft_issue(self, project_id, title, body):
        item_id = f"card-{self.hook}"
        with (Path("remote") / item_id).open("x", encoding="utf-8") as card:
            card.write(f"{title}\n{body}")
        if self.hook == "first":
            wait_for(Path("release-first"))
        return item_id

    def update_item_status(self, project_id, item_id, field_id, option_id):
        with (Path("remote") / item_id).open("a", encoding="utf-8") as card:
            card.write("\n" + option_id)


def run_hook(bash, workspace, name):
    # Supply the changed-file list and the CLI's fake remote to the real hook.
    command = """
        PATH=/usr/bin:$PATH
        if [[ "$OSTYPE" == msys* ]]; then set -o igncr; fi
        git() { echo _kanban.md; }
        kanban-sync() { "$TEST_PYTHON" -m tests.test_sync_hooks "$@"; }
        source "$TEST_POST_COMMIT"
    """
    result = subprocess.run(
        [bash, "-c", command], cwd=workspace, capture_output=True,
        encoding="utf-8", errors="replace", timeout=45,
        env={
            **os.environ,
            "TEST_PYTHON": Path(sys.executable).as_posix(),
            "TEST_POST_COMMIT": (ROOT / "git-hooks/post-commit").as_posix(),
            "TEST_HOOK_NAME": name,
            "PYTHONPATH": str(ROOT),
            "GITHUB_TOKEN": "offline",
            "GITHUB_REPO": "owner/repo",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "Sync complete!" in result.stdout, result.stdout + result.stderr


def test_overlapping_hooks_create_one_bound_card_per_board(tmp_path):
    bash = _resolve_bash()
    first_board = tmp_path / "first-board"
    other_board = tmp_path / "other-board"
    for workspace in (first_board, other_board):
        (workspace / "remote").mkdir(parents=True)
        (workspace / ".env").touch()
        (workspace / "_kanban.md").write_text(
            "## TODO\n* [ ] Shared title\n", encoding="utf-8"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_hook, bash, first_board, "first")
        try:
            wait_for(first_board / "remote/card-first", first)
            # A separate board must finish while the first CREATE is pending.
            run_hook(bash, other_board, "independent")
            second = pool.submit(run_hook, bash, first_board, "second")
            wait_for(first_board / "second-lock-attempt", second)
        finally:
            (first_board / "release-first").touch()
        first.result()
        second.result()

    markers = []
    for workspace, item_id in (
        (first_board, "card-first"), (other_board, "card-independent")
    ):
        cards = list((workspace / "remote").iterdir())
        assert [card.name for card in cards] == [item_id]
        title, marker, status = cards[0].read_text(encoding="utf-8").splitlines()
        assert (title, status) == ("Shared title", "todo-option")
        markers.append(marker)
        saved = json.loads((workspace / ".kanban.json").read_text(encoding="utf-8"))
        assert saved["tasks"] == {"Shared title": {"item_id": item_id, "status": "Todo"}}
    assert markers[0] != markers[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CRT locking")
def test_board_lock_waits_past_one_windows_contention_timeout(tmp_path, monkeypatch):
    import msvcrt

    locking = msvcrt.locking
    contended = True

    def compete(fd, mode, size):
        nonlocal contended
        if mode == msvcrt.LK_LOCK and contended:
            contended = False
            raise OSError(errno.EDEADLK, "lock still held after ten attempts")
        return locking(fd, mode, size)

    monkeypatch.setattr(msvcrt, "locking", compete)
    with kanban_io.kanban_lock(str(tmp_path)):
        assert not contended


if __name__ == "__main__":
    sync_kanban.GitHubClient = FakeProject
    if os.environ["TEST_HOOK_NAME"] == "second":
        # Signal contention without changing the real interprocess lock.
        def observed_lock(workspace):
            Path("second-lock-attempt").touch()
            return kanban_io.kanban_lock(workspace)

        sync_kanban.kanban_lock = observed_lock
    sys.exit(sync_kanban.main())
