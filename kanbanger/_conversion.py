"""Worker for the explicit MCP draft conversion."""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from kanban_io import kanban_lock
from sync_kanban import (
    SCHEMA_VERSION, ConfigurationError, GitHubAPIError, GitHubClient,
    LocalBoard, StateManager, _flatten_local_tasks, _load_cli_dotenv,
)
from kanbanger.tools import (
    ERROR_BOARD_KEY_MISMATCH, ERROR_CONFIGURATION, ERROR_GITHUB_API,
    ERROR_KANBAN_NOT_FOUND, ERROR_READ_FAILED, ERROR_TASK_NOT_FOUND,
    _error, _missing_github_credentials, _ok,
)


@dataclass(frozen=True)
class _Target:
    board_key: str | None
    repository_id: str
    project_id: str
    item_id: str


class _Refusal(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _read_target(board_path: Path, title: str) -> _Target:
    if not board_path.is_file():
        raise _Refusal(ERROR_KANBAN_NOT_FOUND, f"Board not found at {board_path}")
    board = LocalBoard(str(board_path))
    try:
        local = _flatten_local_tasks(board.parse())
    except ConfigurationError as exc:
        raise _Refusal(ERROR_READ_FAILED, str(exc)) from exc

    state = StateManager(str(board_path))
    if not state.state_file.is_file():
        raise ConfigurationError("Conversion requires an existing .kanban.json.")
    saved = state.load(read_only=True)
    version = saved["schema_version"]
    if type(version) is not int or not 0 <= version <= SCHEMA_VERSION:
        raise ConfigurationError("Conversion requires a supported sync state schema version.")
    if saved.get("board_key") != board.board_key:
        raise _Refusal(
            ERROR_BOARD_KEY_MISMATCH, "The board key differs from the one in .kanban.json."
        )

    item_ids = [task["item_id"] for task in saved["tasks"].values()]
    if any(item_id != item_id.strip() for item_id in item_ids):
        raise ConfigurationError("A tracked Project item ID contains extra whitespace in .kanban.json.")
    if len(set(item_ids)) != len(item_ids):
        raise ConfigurationError("Multiple task titles track the same Project item in .kanban.json.")
    item_id = state.get_item_id(title)
    if item_id is None or title not in local:
        raise _Refusal(ERROR_TASK_NOT_FOUND, f"No tracked board task for '{title}'.")
    repo_id = saved.get("repo_node_id")
    if not isinstance(repo_id, str) or not repo_id.strip():
        raise ConfigurationError(
            "Sync state has no valid repository ID. Preview with sync_to_github(dry_run=True), "
            "then run sync_to_github() to bind the repository before converting."
        )
    project_id = saved.get("project_id")
    if not isinstance(project_id, str) or not project_id or project_id != project_id.strip():
        raise ConfigurationError("Conversion requires a valid Project ID in .kanban.json.")
    return _Target(board.board_key, repo_id, project_id, item_id)


def convert_tracked_draft(board_path: Path, title: str) -> str:
    """Convert only if the board and tracked identity still agree after lookup."""
    workspace = str(board_path.parent)
    try:
        with kanban_lock(workspace):
            target = _read_target(board_path, title)
        if error := _missing_github_credentials(os.environ):
            return error
        client = GitHubClient(os.environ["GITHUB_TOKEN"])
        repo_id = client.get_repository_id(os.environ["GITHUB_REPO"])
        if repo_id != target.repository_id:
            raise ConfigurationError("The configured repository differs from the one in .kanban.json.")
        with kanban_lock(workspace):
            if _read_target(board_path, title) != target:
                raise ConfigurationError("The tracked Project item or its binding changed during lookup.")
            number, url = client.convert_draft_to_issue(target.item_id, repo_id)
    except _Refusal as exc:
        return _error(exc.code, str(exc))
    except (OSError, ConfigurationError) as exc:
        return _error(ERROR_CONFIGURATION, str(exc))
    except GitHubAPIError as exc:
        return _error(ERROR_GITHUB_API, str(exc))
    return _ok(item_id=target.item_id, issue_number=number, issue_url=url)


def main() -> str:
    # Requests reads CA, proxy and other settings from its process environment.
    # Use the same cwd and dotenv loader as sync without changing the MCP host.
    try:
        os.chdir(sys.argv[1])
        _load_cli_dotenv()
    except (OSError, ConfigurationError) as exc:
        return _error(ERROR_CONFIGURATION, str(exc))
    return convert_tracked_draft(Path.cwd() / "_kanban.md", json.load(sys.stdin))


if __name__ == "__main__":
    print(main())
