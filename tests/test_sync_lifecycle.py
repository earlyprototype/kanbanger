"""First-sync lifecycle contracts."""

import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import requests

from kanbanger._conversion import convert_tracked_draft
from sync_kanban import (
    ConfigurationError,
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

    assert result.returncode == 1
    assert result.stderr.startswith("Error: Sync state is corrupt:")
    assert result.stdout == ""
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
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: pytest.fail("preview attempted HTTP"),
    )
    from tests.conftest import _StubMCPServer
    from kanbanger.tools import register_tools

    server = _StubMCPServer()
    register_tools(server)
    payload = json.loads(server.tools["sync_to_github"](dry_run=True))

    assert payload["success"] is False
    assert payload["error_code"] == "configuration_error"
    assert state.read_bytes() == corrupt
    assert list(tmp_path.glob(".kanban.json.corrupt-*")) == []


@pytest.mark.parametrize(
    "state_text",
    [
        "[]",
        "{}",
        json.dumps({"tasks": {"Preview task": []}}),
        json.dumps({"tasks": {"Preview task": {}}}),
        json.dumps({
            "tasks": {"Preview task": {"item_id": "", "status": "Todo"}}
        }),
        json.dumps({
            "tasks": {"Preview task": {"item_id": 1, "status": "Todo"}}
        }),
        json.dumps({"tasks": {"Preview task": {"item_id": "PVTI_x"}}}),
        json.dumps({
            "tasks": {"Preview task": {"item_id": "PVTI_x", "status": 1}}
        }),
    ],
)
def test_state_preview_rejects_invalid_structure_without_writing(
    tmp_path, state_text
):
    """A missing dict state/tasks/task record must not become a fresh sync."""
    state_path = tmp_path / ".kanban.json"
    state_path.write_text(state_text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Sync state is corrupt"):
        StateManager(str(tmp_path / "_kanban.md")).load(read_only=True)

    assert state_path.read_text(encoding="utf-8") == state_text
    assert list(tmp_path.glob(".kanban.json.corrupt-*")) == []


def test_state_load_accepts_pending_status_and_extra_fields(tmp_path):
    """A created item may remain pending while its status mutation retries."""
    state_path = tmp_path / ".kanban.json"
    expected = {
        "tasks": {
            "Preview task": {
                "item_id": "PVTI_x",
                "status": None,
                "future_field": "preserved",
            }
        }
    }
    state_path.write_text(json.dumps(expected), encoding="utf-8")

    loaded = StateManager(str(tmp_path / "_kanban.md")).load(read_only=True)

    assert loaded["tasks"] == expected["tasks"]


def test_mcp_preview_rejects_invalid_utf8_state_without_writing(tmp_path, monkeypatch):
    """Invalid state bytes are a structured, read-only configuration failure."""
    (tmp_path / "_kanban.md").write_text("# Board\n\n## TODO\n", encoding="utf-8")
    state_path = tmp_path / ".kanban.json"
    corrupt = b"\xff"
    state_path.write_bytes(corrupt)
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    from tests.conftest import _StubMCPServer
    from kanbanger.tools import register_tools

    server = _StubMCPServer()
    register_tools(server)
    payload = json.loads(server.tools["sync_to_github"](dry_run=True))

    assert payload["error_code"] == "configuration_error"
    assert state_path.read_bytes() == corrupt
    assert list(tmp_path.glob(".kanban.json.corrupt-*")) == []


def test_mcp_preview_reports_invalid_utf8_board_as_read_failure(tmp_path, monkeypatch):
    """A board read failure must remain distinct from state configuration."""
    (tmp_path / "_kanban.md").write_bytes(b"\xff")
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    from tests.conftest import _StubMCPServer
    from kanbanger.tools import register_tools

    server = _StubMCPServer()
    register_tools(server)
    payload = json.loads(server.tools["sync_to_github"](dry_run=True))

    assert payload["error_code"] == "read_failed"


def test_state_load_backs_up_and_resets_invalid_structure(tmp_path, capsys):
    """Real sync preserves corrupt state but starts clean, risking duplicates."""
    state_path = tmp_path / ".kanban.json"
    corrupt = b"[]"
    state_path.write_bytes(corrupt)

    state = StateManager(str(tmp_path / "_kanban.md")).load()

    assert state["tasks"] == {}
    backups = list(tmp_path.glob(".kanban.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == corrupt
    assert "treat the board as never-synced" in capsys.readouterr().err


def test_mcp_preview_import_does_not_load_cwd_or_workspace_dotenv(
    tmp_path, monkeypatch
):
    """Lazy planner imports must not rewrite the MCP server environment."""
    workspace = tmp_path / "workspace"
    cwd = tmp_path / "cwd"
    workspace.mkdir()
    cwd.mkdir()
    (workspace / "_kanban.md").write_text(
        "# Board\n\n## TODO\n* [ ] Preview task\n", encoding="utf-8"
    )
    (workspace / ".env").write_text(
        "KANBANGER_PREVIEW_ENV=workspace\n", encoding="utf-8"
    )
    (cwd / ".env").write_text(
        "KANBANGER_PREVIEW_ENV=cwd\n", encoding="utf-8"
    )
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(workspace))
    monkeypatch.setenv("KANBANGER_PREVIEW_ENV", "process")
    monkeypatch.chdir(cwd)
    monkeypatch.delitem(sys.modules, "sync_kanban", raising=False)
    from tests.conftest import _StubMCPServer
    from kanbanger.tools import register_tools

    server = _StubMCPServer()
    register_tools(server)
    payload = json.loads(server.tools["sync_to_github"](dry_run=True))

    assert payload["success"] is True
    assert os.environ["KANBANGER_PREVIEW_ENV"] == "process"


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


@pytest.fixture
def project_graphql(monkeypatch):
    """Simulate GitHub at the HTTP boundary and record the requests."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    calls = []
    converted_items = set()

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
            assert variables == {"owner": "owner", "repo": "repo"}
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
            return Response({"data": {"archiveProjectV2Item": {
                "item": {"id": variables["itemId"]}
            }}})
        if "convertProjectV2DraftIssueItemToIssue" in query:
            item_id = variables["itemId"]
            if item_id in converted_items:
                return Response({"errors": [{"message": "Item is already an Issue"}]})
            converted_items.add(item_id)
            return Response({"data": {"convertProjectV2DraftIssueItemToIssue": {
                "item": {"id": "PVTI_new", "content": {
                    "number": 42, "url": "https://github.com/owner/repo/issues/42"
                }}
            }}})
        raise AssertionError("remote reconciliation is not part of the sync plan")

    monkeypatch.setattr("requests.post", fake_post)
    return calls


def test_sync_executes_local_plan_without_remote_reconciliation(tmp_path, project_graphql):
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
    calls = project_graphql
    Syncer(LocalBoard(str(board_path)), StateManager(str(board_path)), GitHubClient("token")).sync(
        "owner/repo", 8
    )

    saved = StateManager(str(board_path)).load()["tasks"]
    assert saved["New"]["status"] == "Todo"
    assert saved["Moved"]["status"] == "Review"
    assert "Removed" not in saved
    assert [c["variables"] for c in calls if "archiveProjectV2Item" in c["query"]] == [
        {"projectId": "PVT_project", "itemId": "PVTI_removed"}
    ]
    update_vars = [
        call["variables"] for call in calls
        if "updateProjectV2ItemFieldValue" in call["query"]
    ]
    assert update_vars == [
        {"projectId": "PVT_project", "itemId": "PVTI_new", "fieldId": "PVTSSF_status", "optionId": "opt_todo"},
        {"projectId": "PVT_project", "itemId": "PVTI_moved", "fieldId": "PVTSSF_status", "optionId": "opt_review"},
    ]


def test_explicit_conversion_preserves_item_for_move_and_archive(
    kanban_workspace, registered_tools, project_graphql, monkeypatch
):
    board = kanban_workspace / "_kanban.md"
    post = requests.post

    def check_mutation_lock(*args, **kwargs):
        if "mutation" in kwargs["json"]["query"]:
            # A second workspace writer must be excluded during each remote mutation.
            with (kanban_workspace / ".kanban.lock").open("r+b") as lock:
                with pytest.raises(OSError) as busy:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                assert busy.value.errno in (errno.EACCES, errno.EAGAIN)
        return post(*args, **kwargs)

    monkeypatch.setattr(requests, "post", check_mutation_lock)
    state = StateManager(str(board))
    syncer = Syncer(LocalBoard(str(board)), state, GitHubClient("test-token"))
    registered_tools["add_task"]("Unrelated")
    state.set_project_info("R_repo", "PVT_project")
    state.update_task("Unrelated", "PVTI_unrelated", "Todo")
    state.save()
    registered_tools["add_task"]("New")
    syncer.sync("owner/repo", 8)
    assert not any("convertProject" in c["query"] for c in project_graphql)
    original = state.state_file.read_bytes()
    project_graphql.clear()

    result = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))
    assert result == {
        "success": True, "item_id": "PVTI_new", "issue_number": 42,
        "issue_url": "https://github.com/owner/repo/issues/42",
    }
    lookup, conversion = project_graphql
    assert "repository(owner" in lookup["query"]
    mutation = " ".join(conversion["query"].split())
    assert "convertProjectV2DraftIssueItemToIssue" in mutation
    assert "itemId: $itemId" in mutation and "repositoryId: $repositoryId" in mutation
    assert "... on Issue { number url }" in mutation
    assert conversion["variables"] == {"itemId": "PVTI_new", "repositoryId": "R_repo"}
    assert state.state_file.read_bytes() == original

    repeated = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))
    assert repeated["success"] is False
    assert repeated["error_code"] == "github_api_error"
    assert state.state_file.read_bytes() == original

    project_graphql.clear()
    registered_tools["move_task"]("New", "TODO", "REVIEW")
    syncer.sync("owner/repo", 8)
    registered_tools["delete_task"]("New", "REVIEW")
    syncer.sync("owner/repo", 8)
    mutations = [c for c in project_graphql if "mutation" in c["query"]]
    move, archive = mutations
    assert "updateProjectV2ItemFieldValue" in move["query"]
    assert move["variables"] == {
        "projectId": "PVT_project", "itemId": "PVTI_new",
        "fieldId": "PVTSSF_status", "optionId": "opt_review",
    }
    assert "archiveProjectV2Item" in archive["query"]
    assert archive["variables"] == {"projectId": "PVT_project", "itemId": "PVTI_new"}
    assert state.load()["tasks"] == {
        "Unrelated": {"item_id": "PVTI_unrelated", "status": "Todo"},
    }


@pytest.mark.parametrize(("saved", "error_code"), [
    (None, "configuration_error"),
    (b"{", "configuration_error"),
    (b'{"tasks":{"New":{"status":"Todo"}}}', "configuration_error"),
    (b'{"repo_node_id":"R_repo","tasks":{"New":{"item_id":"PVTI_first","status":"Todo"},'
     b'"New":{"item_id":"PVTI_new","status":"Todo"}}}', "configuration_error"),
    (b'{"tasks":{}, "repo_node_id":"R_repo"}', "task_not_found"),
])
def test_conversion_refuses_missing_corrupt_or_untracked_state(
    kanban_workspace, registered_tools, project_graphql, saved, error_code
):
    registered_tools["add_task"]("New")
    path = kanban_workspace / ".kanban.json"
    if saved is not None:
        path.write_bytes(saved)
    result = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))
    assert result["success"] is False
    assert result["error_code"] == error_code
    assert project_graphql == []
    assert (path.read_bytes() if path.exists() else None) == saved
    assert list(kanban_workspace.glob(".kanban.json.corrupt-*")) == []


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_conversion_refuses_non_json_constants(
    kanban_workspace, registered_tools, project_graphql, constant
):
    registered_tools["add_task"]("New")
    path = kanban_workspace / ".kanban.json"
    original = (
        b'{"schema_version":1,"repo_node_id":"R_repo","project_id":"PVT_project",'
        b'"tasks":{"New":{"item_id":"PVTI_new","status":"Todo","extra":%s}}}'
        % constant
    )
    path.write_bytes(original)

    result = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))

    assert result["success"] is False
    assert result["error_code"] == "configuration_error"
    assert project_graphql == []
    assert path.read_bytes() == original


def test_conversion_refuses_an_item_tracked_by_two_titles(
    kanban_workspace, registered_tools, project_graphql
):
    state = StateManager(kanban_workspace / "_kanban.md")
    state.set_project_info("R_repo", "PVT_project")
    state.update_task("Original", "PVTI_new", "Todo")
    state.update_task("New", "PVTI_new", "Todo")
    state.save()
    before = state.state_file.read_bytes()

    result = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))

    assert result["success"] is False
    assert result["error_code"] == "configuration_error"
    assert project_graphql == []
    assert state.state_file.read_bytes() == before


@pytest.mark.parametrize("defect", [
    "invalid_project", "unrelated_duplicate", "invalid_version", "null_version", "future_version",
])
def test_conversion_validates_the_whole_binding(
    kanban_workspace, registered_tools, project_graphql, defect
):
    registered_tools["add_task"]("New")
    state = StateManager(kanban_workspace / "_kanban.md")
    state.set_project_info("R_repo", "PVT_project")
    state.update_task("New", "PVTI_new", "Todo")
    if defect == "invalid_project":
        state.state["project_id"] = {"id": "PVT_project"}
    elif defect == "unrelated_duplicate":
        state.update_task("Other", "PVTI_shared", "Review")
        state.update_task("Third", "PVTI_shared", "Done")
    elif defect == "invalid_version":
        state.state["schema_version"] = True
    elif defect == "null_version":
        state.state["schema_version"] = None
    else:
        state.state["schema_version"] = 2
    state.save()
    before = state.state_file.read_bytes()

    result = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))

    assert result["success"] is False
    assert result["error_code"] == "configuration_error"
    assert project_graphql == []
    assert state.state_file.read_bytes() == before


def test_conversion_accepts_legacy_state_without_schema_version(
    kanban_workspace, registered_tools, project_graphql
):
    registered_tools["add_task"]("New")
    state = StateManager(kanban_workspace / "_kanban.md")
    state.set_project_info("R_repo", "PVT_project")
    state.update_task("New", "PVTI_new", "Todo")
    del state.state["schema_version"]
    state.save()
    before = state.state_file.read_bytes()

    result = json.loads(convert_tracked_draft(state.kanban_file, "New"))

    assert result["success"] is True
    assert result["item_id"] == "PVTI_new"
    assert state.state_file.read_bytes() == before


@pytest.mark.parametrize("recorded_key", [None, "cd" * 16])
def test_keyed_board_with_legacy_or_conflicting_state(
    kanban_workspace, registered_tools, project_graphql, recorded_key
):
    board_key = "ab" * 16
    board = kanban_workspace / "_kanban.md"
    board.write_text(
        f"<!-- kanbanger:board-id: {board_key} -->\n## TODO\n* [ ] New\n",
        encoding="utf-8",
    )
    saved = {
        "project_id": "PVT_project",
        "tasks": {"New": {"item_id": "PVTI_new", "status": "Todo"}},
    }
    if recorded_key is not None:
        saved["board_key"] = recorded_key
    path = kanban_workspace / ".kanban.json"
    original = json.dumps(saved).encode()
    path.write_bytes(original)

    result = json.loads(convert_tracked_draft(board, "New"))

    assert result["success"] is False
    assert project_graphql == []
    assert path.read_bytes() == original
    if recorded_key is not None:
        assert result["error_code"] == "board_key_mismatch"
        assert "sync_to_github()" not in result["message"]
        return

    assert result["error_code"] == "configuration_error"
    message = result["message"]
    assert message.index("sync_to_github(dry_run=True)") < message.index("sync_to_github()")
    preview = json.loads(registered_tools["sync_to_github"](dry_run=True))
    assert preview["success"] is True
    assert path.read_bytes() == original
    state = StateManager(board)
    Syncer(LocalBoard(str(board)), state, GitHubClient("test-token")).sync("owner/repo", 8)
    assert state.load()["board_key"] == board_key
    assert state.state["repo_node_id"] == "R_repo"
    assert json.loads(convert_tracked_draft(board, "New"))["success"] is True


@pytest.mark.parametrize("change", [
    "remove_mapping", "replace_mapping", "remove_from_board", "remove_state_key",
])
def test_conversion_refuses_a_changed_target_after_repository_lookup(
    kanban_workspace, registered_tools, project_graphql, monkeypatch, change
):
    registered_tools["add_task"]("New")
    state = StateManager(kanban_workspace / "_kanban.md")
    board_key = "ab" * 16
    state.kanban_file.write_text(
        f"<!-- kanbanger:board-id: {board_key} -->\n" + state.kanban_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    state.state["board_key"] = board_key
    state.set_project_info("R_repo", "PVT_project")
    state.update_task("New", "PVTI_new", "Todo")
    state.save()
    post = requests.post
    after_change = []

    def change_during_lookup(*args, **kwargs):
        response = post(*args, **kwargs)
        if "repository(owner" in kwargs["json"]["query"]:
            if change == "remove_mapping":
                state.remove_task("New")
                state.save()
            elif change == "replace_mapping":
                state.update_task("New", "PVTI_replacement", "Todo")
                state.save()
            elif change == "remove_state_key":
                state.state.pop("board_key")
                state.save()
            else:
                registered_tools["delete_task"]("New", "TODO")
            after_change.append(state.state_file.read_bytes())
        return response

    monkeypatch.setattr(requests, "post", change_during_lookup)
    result = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))
    assert len(project_graphql) == 1
    assert result["success"] is False
    assert result["error_code"] in {"task_not_found", "configuration_error", "board_key_mismatch"}
    assert after_change == [state.state_file.read_bytes()]


def test_sync_and_conversion_use_workspace_github_settings(
    kanban_workspace, registered_tools, monkeypatch
):
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setenv("GITHUB_REPO", "server/other")
    monkeypatch.setenv("GITHUB_TOKEN", "server-token")
    monkeypatch.setenv("GITHUB_PROJECT_NUMBER", "99")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "server-ca.pem")
    monkeypatch.setenv("HTTPS_PROXY", "http://server.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("NETRC", str(kanban_workspace / "absent-netrc"))
    (kanban_workspace / ".env").write_text(
        "GITHUB_REPO=owner/repo\nGITHUB_TOKEN=workspace-token\nGITHUB_PROJECT_NUMBER=8\n"
        "REQUESTS_CA_BUNDLE=certs/ca.pem\nHTTPS_PROXY=http://workspace.invalid:8080\n",
        encoding="utf-8",
    )
    registered_tools["add_task"]("New")
    state = StateManager(kanban_workspace / "_kanban.md")
    state.update_task("New", "PVTI_new", "Todo")
    state.save()

    # Exercise Requests' environment merging in the two actual worker processes.
    source_root = Path(__file__).resolve().parents[1]
    import_path = os.pathsep.join([str(kanban_workspace), str(source_root)])
    monkeypatch.setenv("PYTHONPATH", import_path, prepend=os.pathsep)
    log = kanban_workspace / "requests.jsonl"
    monkeypatch.setenv("TEST_REQUEST_LOG", str(log))
    adapter = textwrap.dedent("""\
        import json, os
        from pathlib import Path
        from requests import Response
        from requests.adapters import HTTPAdapter

        def send(self, request, **settings):
            body = json.loads(request.body)
            path = str(Path(settings["verify"]).resolve())
            with open(os.environ["TEST_REQUEST_LOG"], "a", encoding="utf-8") as stream:
                print(json.dumps([body["variables"], request.headers["Authorization"],
                                  path, settings["proxies"]["https"]]), file=stream)
            project = {"id": "PVT_project", "number": 8, "fields": {"nodes": [
                {"id": "field", "name": "Status", "options": []}]}}
            repo = {"id": "R_repo", "projectsV2": {"nodes": [project]}}
            item = {"id": "PVTI_new", "content": {"number": 42, "url": "https://example.org/42"}}
            data = {"repository": repo, "convertProjectV2DraftIssueItemToIssue": {"item": item}}
            response = Response()
            response.status_code = 200
            response._content = json.dumps({"data": data}).encode()
            return response

        HTTPAdapter.send = send
        """)
    (kanban_workspace / "sitecustomize.py").write_text(adapter, encoding="utf-8")
    sync_result = registered_tools["sync_to_github"]()
    assert sync_result.startswith("Sync complete"), sync_result
    assert state.load()["repo_node_id"] == "R_repo"

    result = json.loads(registered_tools["convert_task_to_issue"]("New"))
    assert result["success"] is True
    assert result["item_id"] == "PVTI_new"
    assert os.environ["GITHUB_REPO"] == "server/other"
    assert os.environ["GITHUB_TOKEN"] == "server-token"
    assert os.environ["GITHUB_PROJECT_NUMBER"] == "99"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row[0] for row in rows] == [
        {"owner": "owner", "repo": "repo"}, {"owner": "owner", "repo": "repo"},
        {"itemId": "PVTI_new", "repositoryId": "R_repo"},
    ]
    assert [row[1:] for row in rows] == [[
        "Bearer workspace-token", str(kanban_workspace / "certs" / "ca.pem"),
        "http://workspace.invalid:8080",
    ]] * 3
    assert os.environ["REQUESTS_CA_BUNDLE"] == "server-ca.pem"


@pytest.mark.parametrize("missing", ["GITHUB_REPO", "GITHUB_TOKEN"])
def test_workspace_dotenv_cannot_satisfy_mcp_preflight(
    kanban_workspace, registered_tools, project_graphql, monkeypatch, missing
):
    state = StateManager(kanban_workspace / "_kanban.md")
    state.set_project_info("R_repo", "PVT_project")
    state.update_task("New", "PVTI_new", "Todo")
    state.save()
    before = state.state_file.read_bytes()
    (kanban_workspace / ".env").write_text(
        "GITHUB_REPO=owner/repo\nGITHUB_TOKEN=workspace-token\n", encoding="utf-8"
    )
    monkeypatch.delenv(missing)

    for result in (registered_tools["sync_to_github"](),
                   registered_tools["convert_task_to_issue"]("New")):
        assert json.loads(result)["error_code"] == "missing_" + missing.lower()
    assert project_graphql == []
    assert state.state_file.read_bytes() == before


@pytest.mark.parametrize("repo_id", [None, "", "R_other"])
def test_conversion_refuses_unbound_or_mismatched_repository(
    kanban_workspace, registered_tools, project_graphql, repo_id
):
    registered_tools["add_task"]("New")
    saved = {"project_id": "PVT_project", "tasks": {"New": {"item_id": "PVTI_new", "status": "Todo"}}}
    if repo_id is not None:
        saved["repo_node_id"] = repo_id
    path = kanban_workspace / ".kanban.json"
    original = json.dumps(saved).encode()
    path.write_bytes(original)
    result = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))
    assert result["success"] is False
    assert result["error_code"] == "configuration_error"
    if repo_id:
        lookup, = project_graphql
        assert "repository(owner" in lookup["query"]
    else:
        assert project_graphql == []
        assert "sync_to_github(dry_run=True)" in result["message"]
        assert "sync_to_github()" in result["message"]
    assert path.read_bytes() == original


@pytest.mark.parametrize("saved_repo", [None, "R_original"])
def test_sync_binds_legacy_state_but_preserves_an_existing_repository_binding(
    kanban_workspace, registered_tools, project_graphql, saved_repo
):
    registered_tools["add_task"]("New")
    board = kanban_workspace / "_kanban.md"
    state = StateManager(str(board))
    state.update_task("New", "PVTI_new", "Todo")
    state.set_project_info(saved_repo, "PVT_project")
    state.save()
    before = state.state_file.read_bytes()
    syncer = Syncer(LocalBoard(str(board)), state, GitHubClient("test-token"))
    if saved_repo is None:
        syncer.sync("owner/repo", 8)
        assert state.load()["repo_node_id"] == "R_repo"
        assert json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))["success"]
    else:
        with pytest.raises(ConfigurationError, match="repository"):
            syncer.sync("owner/repo", 8)
        result = json.loads(convert_tracked_draft(kanban_workspace / "_kanban.md", "New"))
        assert result["success"] is False
        assert state.state_file.read_bytes() == before
        assert all("mutation" not in c["query"] for c in project_graphql)


@pytest.mark.parametrize(("saved_repo", "status"), [
    (None, "Todo"), ("R_repo", "Todo"), ("R_repo", "Review"),
])
def test_sync_refuses_to_bind_tracked_items_through_a_different_project(
    kanban_workspace, registered_tools, project_graphql, saved_repo, status
):
    registered_tools["add_task"]("New")
    board = kanban_workspace / "_kanban.md"
    state = StateManager(str(board))
    state.update_task("New", "PVTI_new", status)
    state.set_project_info(saved_repo, "PVT_original")
    state.save()
    before = state.state_file.read_bytes()

    syncer = Syncer(LocalBoard(str(board)), state, GitHubClient("test-token"))
    with pytest.raises(ConfigurationError, match="Project"):
        syncer.sync("owner/repo", 8)
    assert state.state_file.read_bytes() == before
    lookup, = project_graphql
    assert "repository(owner" in lookup["query"]


def test_created_item_is_durable_before_initial_status_failure(tmp_path):
    """A failed status mutation must retry UPDATE, not create a duplicate item."""
    board_path = tmp_path / "_kanban.md"
    board_path.write_text(
        "# Board\n\n## TODO\n* [ ] New\n", encoding="utf-8"
    )

    class Client:
        @staticmethod
        def get_repo_project(owner, repo, project_number):
            return "R_repo", "PVT_project", "PVTSSF_status", {"Todo": "opt_todo"}

        @staticmethod
        def create_draft_issue(project_id, title):
            return "PVTI_new"

        @staticmethod
        def update_item_status(*args):
            raise GitHubAPIError("status failed")

    with pytest.raises(GitHubAPIError, match="status failed"):
        Syncer(
            LocalBoard(str(board_path)), StateManager(str(board_path)), Client()
        ).sync("owner/repo", 8)

    reloaded = StateManager(str(board_path)).load()["tasks"]
    assert reloaded["New"] == {"item_id": "PVTI_new", "status": None}
    assert build_sync_plan({"New": "Todo"}, reloaded)["operations"] == [{
        "action": "UPDATE",
        "title": "New",
        "from_status": None,
        "to_status": "Todo",
        "item_id": "PVTI_new",
    }]


def test_archive_failure_keeps_state_for_retry(tmp_path):
    """A failed archive must leave its item ID durable for the next sync."""
    board_path = tmp_path / "_kanban.md"
    board_path.write_text("# Board\n\n## TODO\n", encoding="utf-8")
    state = StateManager(str(board_path))
    state.state["tasks"] = {
        "Removed": {"item_id": "PVTI_removed", "status": "Done"}
    }
    state.save()

    class Client:
        @staticmethod
        def get_repo_project(owner, repo, project_number):
            return "R_repo", "PVT_project", "PVTSSF_status", {}

        @staticmethod
        def archive_item(project_id, item_id):
            raise GitHubAPIError("archive failed")

    with pytest.raises(GitHubAPIError, match="archive failed"):
        Syncer(
            LocalBoard(str(board_path)), StateManager(str(board_path)), Client()
        ).sync("owner/repo", 8)

    assert StateManager(str(board_path)).load()["tasks"] == {
        "Removed": {"item_id": "PVTI_removed", "status": "Done"}
    }


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


def test_invalid_header_error_omits_the_authorization_value(monkeypatch):
    def reject_header(url, *, headers, **kwargs):
        raise requests.exceptions.InvalidHeader(headers["Authorization"])

    monkeypatch.setattr(requests, "post", reject_header)
    with pytest.raises(GitHubAPIError, match="GitHub API request failed") as caught:
        GitHubClient("test-credential").get_repository_id("owner/repo")
    assert "test-credential" not in str(caught.value)


def test_token_with_a_trailing_newline_fails_before_any_request(project_graphql):
    with pytest.raises(ConfigurationError, match="token contains a line break") as caught:
        GitHubClient("test-credential\n").get_repository_id("owner/repo")
    assert "test-credential" not in str(caught.value)
    assert project_graphql == []


def test_github_client_rejects_a_non_json_response(monkeypatch):
    """A 200 HTML/error body must cross the client boundary as GitHubAPIError."""
    class Response:
        status_code = 200
        text = "not json"

        @staticmethod
        def json():
            raise ValueError("invalid JSON")

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(GitHubAPIError, match="invalid JSON") as caught:
        GitHubClient("token")._query("query { viewer { login } }", {})

    assert isinstance(caught.value.__cause__, ValueError)


def test_github_client_rejects_a_non_object_json_response(monkeypatch):
    """A successful response must still match GraphQL's object envelope."""
    class Response:
        status_code = 200
        text = "[]"

        @staticmethod
        def json():
            return []

    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: Response())

    with pytest.raises(GitHubAPIError, match="JSON that is not an object"):
        GitHubClient("token")._query("query { viewer { login } }", {})


def test_mcp_classifies_github_request_failures():
    """Translated transport failures must retain the GitHub API error code."""
    from kanbanger.tools import ERROR_GITHUB_API, _classify_sync_stderr

    assert _classify_sync_stderr(
        "Error: GitHub API request failed: timed out\n"
    ) == ERROR_GITHUB_API
    assert _classify_sync_stderr(
        "Error: GitHub API returned invalid JSON: bad body\n"
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
