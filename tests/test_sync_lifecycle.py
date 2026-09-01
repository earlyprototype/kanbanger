"""First-sync lifecycle contracts."""

import json
import os
import subprocess
import sys

import pytest
import requests

from sync_kanban import (
    GitHubAPIError,
    GitHubClient,
    LocalBoard,
    StateManager,
    Syncer,
    build_sync_plan,
)


def test_build_sync_plan_orders_local_changes_before_removed_state():
    """A missing action or wrong status direction corrupts the remote board."""
    local = {"New": "Todo", "Moved": "Review"}
    state = {
        "Moved": {"item_id": "PVTI_moved", "status": "InProgress"},
        "Removed": {"item_id": "PVTI_removed", "status": "Done"},
    }
    assert build_sync_plan(local, state) == {
        "operations": [
            {
                "action": "CREATE",
                "title": "New",
                "from_status": None,
                "to_status": "Todo",
                "item_id": None,
            },
            {
                "action": "UPDATE",
                "title": "Moved",
                "from_status": "InProgress",
                "to_status": "Review",
                "item_id": "PVTI_moved",
            },
            {
                "action": "ARCHIVE",
                "title": "Removed",
                "from_status": "Done",
                "to_status": None,
                "item_id": "PVTI_removed",
            },
        ],
        "summary": {"create": 1, "update": 1, "archive": 1},
    }


def test_dry_run_prints_plan_without_github_configuration(tmp_path):
    """A preview must stay local even when no GitHub credentials exist."""
    board = tmp_path / "_kanban.md"
    board.write_text("# Board\n\n## TODO\n* [ ] Preview task\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITHUB_REPO", None)

    result = subprocess.run(
        [sys.executable, "-m", "sync_kanban", str(board), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["operations"][0]["action"] == "CREATE"
    assert not (tmp_path / ".kanban.json").exists()


def test_cli_preview_does_not_recover_corrupt_state_on_disk(tmp_path):
    """Previewing corrupt state must not create a backup or alter the source."""
    board = tmp_path / "_kanban.md"
    board.write_text("# Board\n\n## TODO\n* [ ] Preview task\n", encoding="utf-8")
    state = tmp_path / ".kanban.json"
    corrupt = b"{ broken"
    state.write_bytes(corrupt)

    result = subprocess.run(
        [sys.executable, "-m", "sync_kanban", str(board), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert state.read_bytes() == corrupt
    assert list(tmp_path.glob(".kanban.json.corrupt-*")) == []


def test_mcp_preview_does_not_recover_corrupt_state_on_disk(tmp_path, monkeypatch):
    """The MCP preview path has the same no-write contract as the CLI."""
    board = tmp_path / "_kanban.md"
    board.write_text("# Board\n\n## TODO\n* [ ] Preview task\n", encoding="utf-8")
    state = tmp_path / ".kanban.json"
    corrupt = b"{ broken"
    state.write_bytes(corrupt)
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    from tests.conftest import _StubMCPServer
    from kanbanger.tools import register_tools

    server = _StubMCPServer()
    register_tools(server)
    payload = json.loads(server.tools["sync_to_github"](dry_run=True))

    assert payload["success"] is True
    assert state.read_bytes() == corrupt
    assert list(tmp_path.glob(".kanban.json.corrupt-*")) == []


def test_cli_preview_rejects_copied_board_state(tmp_path):
    """Preview must not offer a plan that real sync refuses to execute."""
    board = tmp_path / "_kanban.md"
    board.write_text(
        "# Board\n<!-- kanbanger:board-id: " + "b" * 32 + " -->\n\n## TODO\n",
        encoding="utf-8",
    )
    (tmp_path / ".kanban.json").write_text(
        json.dumps({"schema_version": 1, "board_key": "a" * 32, "tasks": {}}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "sync_kanban", str(board), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stderr.startswith("Error: sync state belongs to a different board")
    assert result.stdout == ""


def test_mcp_preview_rejects_copied_board_state(tmp_path, monkeypatch):
    """MCP preview returns the existing structured copied-board refusal."""
    board = tmp_path / "_kanban.md"
    board.write_text(
        "# Board\n<!-- kanbanger:board-id: " + "b" * 32 + " -->\n\n## TODO\n",
        encoding="utf-8",
    )
    (tmp_path / ".kanban.json").write_text(
        json.dumps({"schema_version": 1, "board_key": "a" * 32, "tasks": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    from tests.conftest import _StubMCPServer
    from kanbanger.tools import register_tools

    server = _StubMCPServer()
    register_tools(server)
    payload = json.loads(server.tools["sync_to_github"](dry_run=True))

    assert payload["success"] is False
    assert payload["error_code"] == "board_key_mismatch"


def test_sync_executes_local_plan_without_remote_reconciliation(tmp_path, monkeypatch):
    """A remote fetch must not decide what local state creates, moves, or archives."""
    board_path = tmp_path / "_kanban.md"
    board_path.write_text(
        "# Board\n\n## TODO\n* [ ] New\n\n## REVIEW\n* [ ] Moved\n",
        encoding="utf-8",
    )
    state = StateManager(str(board_path))
    state.state["tasks"] = {
        "Moved": {"item_id": "PVTI_moved", "status": "InProgress"},
        "Removed": {"item_id": "PVTI_removed", "status": "Done"},
    }
    state.save()
    calls = []

    class Response:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_post(url, *, headers, json, timeout=None):
        calls.append(json)
        query = json["query"]
        variables = json["variables"]
        if "repository(owner" in query:
            return Response({"data": {"repository": {
                "id": "R_repo",
                "projectsV2": {"nodes": [{
                    "id": "PVT_project", "number": 8, "title": "Project",
                    "fields": {"nodes": [{
                        "id": "PVTSSF_status", "name": "Status",
                        "options": [
                            {"id": "opt_todo", "name": "Todo"},
                            {"id": "opt_review", "name": "Review"},
                            {"id": "opt_done", "name": "Done"},
                        ],
                    }]},
                }]},
            }}})
        if "addProjectV2DraftIssue" in query:
            assert variables == {
                "projectId": "PVT_project", "title": "New", "body": ""
            }
            return Response({"data": {"addProjectV2DraftIssue": {
                "projectItem": {"id": "PVTI_new"}
            }}})
        if "updateProjectV2ItemFieldValue" in query:
            return Response({"data": {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": variables["itemId"]}
            }}})
        if "archiveProjectV2Item" in query:
            assert variables == {"projectId": "PVT_project", "itemId": "PVTI_removed"}
            return Response({"data": {"archiveProjectV2Item": {
                "item": {"id": "PVTI_removed"}
            }}})
        raise AssertionError("remote reconciliation is not part of the sync plan")

    monkeypatch.setattr("requests.post", fake_post)
    Syncer(LocalBoard(str(board_path)), StateManager(str(board_path)), GitHubClient("token")).sync(
        "owner/repo", 8
    )

    saved = StateManager(str(board_path)).load()["tasks"]
    assert saved["New"]["status"] == "Todo"
    assert saved["Moved"]["status"] == "Review"
    assert "Removed" not in saved
    update_vars = [
        call["variables"] for call in calls
        if "updateProjectV2ItemFieldValue" in call["query"]
    ]
    assert update_vars == [
        {"projectId": "PVT_project", "itemId": "PVTI_new", "fieldId": "PVTSSF_status", "optionId": "opt_todo"},
        {"projectId": "PVT_project", "itemId": "PVTI_moved", "fieldId": "PVTSSF_status", "optionId": "opt_review"},
    ]


def test_sync_status_resource_reads_task_state_like_the_tool(tmp_path, monkeypatch):
    """Changing the state key must not make the resource disagree with its tool."""
    (tmp_path / ".kanban.json").write_text(json.dumps({"tasks": {
        "One": {"item_id": "PVTI_one", "status": "Todo"},
        "Two": {"item_id": "PVTI_two", "status": "Review"},
    }}), encoding="utf-8")
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    from tests.conftest import _StubMCPServer
    from kanbanger.resources import register_resources
    from kanbanger.tools import register_tools

    server = _StubMCPServer()
    register_resources(server)
    register_tools(server)
    resource = json.loads(server.resources["github_sync_status"]())
    tool = json.loads(server.tools["get_sync_status"]())

    assert resource["synced_tasks"] == tool["synced_tasks"] == 2
    assert resource["local_task_titles"] == tool["github_items"] == ["One", "Two"]
    assert resource["github_item_ids"] == tool["github_item_ids"] == ["PVTI_one", "PVTI_two"]


def test_github_client_uses_a_finite_request_timeout(monkeypatch):
    """A stalled GitHub connection must not block sync forever."""
    received = []

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"data": {}}

    def fake_post(url, *, headers, json, timeout=None):
        received.append(timeout)
        return Response()

    monkeypatch.setattr("requests.post", fake_post)
    GitHubClient("token")._query("query { viewer { login } }", {})

    assert len(received) == 1
    assert isinstance(received[0], (int, float)) and 0 < received[0] < float("inf")


def test_github_client_translates_request_failures(monkeypatch):
    """Transport failures must cross the client boundary as GitHubAPIError."""
    def fail_post(*args, **kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr("requests.post", fail_post)

    with pytest.raises(GitHubAPIError, match="GitHub API request failed") as caught:
        GitHubClient("token")._query("query { viewer { login } }", {})

    assert isinstance(caught.value.__cause__, requests.Timeout)


def test_mcp_classifies_github_request_failures():
    """Translated transport failures must retain the GitHub API error code."""
    from kanbanger.tools import ERROR_GITHUB_API, _classify_sync_stderr

    assert _classify_sync_stderr(
        "Error: GitHub API request failed: timed out\n"
    ) == ERROR_GITHUB_API


def test_malformed_repo_is_a_formatted_configuration_error(tmp_path):
    """Malformed nonempty repo config must not leak split ValueError traces."""
    board = tmp_path / "_kanban.md"
    board.write_text("# Board\n\n## TODO\n", encoding="utf-8")
    env = dict(os.environ)
    env["GITHUB_TOKEN"] = "test-token"

    result = subprocess.run(
        [sys.executable, "-m", "sync_kanban", str(board), "--repo", "owner//repo"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 1
    assert result.stderr.startswith("Error: GITHUB_REPO must be 'owner/repo'")
    assert "Traceback" not in result.stderr


def test_installed_sync_command_formats_expected_errors_without_tracebacks(tmp_path):
    """Console entry points must show recoverable user errors, not internals."""
    board = tmp_path / "_kanban.md"
    board.write_text("# Board\n\n## TODO\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITHUB_REPO", None)

    command = [
        sys.executable, "-c",
        "from sync_kanban import main; raise SystemExit(main())",
    ]
    missing = subprocess.run(
        [*command, str(tmp_path / "missing.md")], capture_output=True,
        text=True, env=env, timeout=30,
    )
    unconfigured = subprocess.run(
        [*command, str(board)], capture_output=True, text=True, env=env, timeout=30,
    )

    for result in (missing, unconfigured):
        assert result.returncode == 1
        assert result.stderr.startswith("Error: ")
        assert "Traceback" not in result.stderr
