"""Exercise ownership recovery with a stateful GitHub Project double."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
import os
import subprocess
import sys
from threading import Event

import pytest

from sync_kanban import ConfigurationError, GitHubAPIError, GitHubClient, LocalBoard, StateManager, Syncer


KEY = "a" * 32


class Project:
    def __init__(self):
        self.items = []
        self.writes = []

    def get_repo_project(self, *args):
        return "repo", "project", "status", {"Todo": "Todo", "Done": "Done"}

    def get_project_items(self, project_id):
        return deepcopy(self.items)

    def create_draft_issue(self, project_id, title, body=""):
        item_id = f"item-{len(self.items)}"
        self.items.append(dict(id=item_id, draft_id="draft-" + item_id,
                               title=title, body=body, status=None, archived=False))
        self.writes.append(("create", item_id))
        return item_id

    def update_item_status(self, project_id, item_id, field, option):
        next(item for item in self.items if item["id"] == item_id)["status"] = option
        self.writes.append(("update", item_id))

    def archive_item(self, project_id, item_id):
        next(item for item in self.items if item["id"] == item_id)["archived"] = True
        self.writes.append(("archive", item_id))

    def mark_draft(self, draft_id, body):
        next(item for item in self.items if item["draft_id"] == draft_id)["body"] = body
        self.writes.append(("mark", draft_id))


def board_at(path, rows):
    path.write_text(f"# Board\n<!-- kanbanger:board-id: {KEY} -->\n{rows}", encoding="utf-8")


def run(board, project, **options):
    return Syncer(LocalBoard(str(board)), StateManager(str(board)), project).sync(
        "owner/repo", 8, **options
    )


@pytest.mark.parametrize("corrupt", [False, True])
def test_recovery_round_trip(tmp_path, corrupt):
    board = tmp_path / "_kanban.md"
    path = tmp_path / ".kanban.json"
    project = Project()
    board_at(board, "## TODO\n* [ ] Café - review\n* [ ] Removed\n")
    run(board, project)
    first, removed = project.items
    assert first["body"] and removed["body"] and first["body"] != removed["body"]
    first["title"] = "Edited on GitHub"
    project.create_draft_issue("project", "Removed", "Someone else's notes")
    unrelated = deepcopy(project.items[-1])
    board_at(board, "## DONE\n* [ ] Café - review\n")
    path.unlink()
    if corrupt:
        path.write_bytes(b"\xffbroken")
    project.writes.clear()

    with pytest.raises(ConfigurationError, match="recover"):
        run(board, project)
    preview = run(board, project, recover=True, preview=True)
    project.items.reverse()
    assert run(board, project, recover=True, preview=True) == preview
    assert preview["plan"]["summary"] == {"create": 0, "update": 1, "archive": 1}
    assert [(op["action"], op["item_id"]) for op in preview["plan"]["operations"]] == [
        ("UPDATE", first["id"]), ("ARCHIVE", removed["id"]),
    ]
    if corrupt:
        assert path.read_bytes() == b"\xffbroken"
    else:
        assert not path.exists()
    assert list(tmp_path.glob(".kanban.json.corrupt-*")) == []
    run(board, project, recover=True)
    assert project.writes == []
    if corrupt:
        backup, = tmp_path.glob(".kanban.json.corrupt-*")
        assert backup.read_bytes() == b"\xffbroken"
    run(board, project)
    run(board, project)
    assert project.writes == [("update", first["id"]), ("archive", removed["id"])]
    assert project.items[0] == unrelated


def test_duplicate_markers_refuse_the_entire_plan(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] One\n")
    project = Project()
    run(board, project)
    project.create_draft_issue("project", "Different title", project.items[0]["body"])
    board_at(board, "## TODO\n* [ ] New\n")
    path = tmp_path / ".kanban.json"
    before = path.read_bytes()
    project.writes.clear()
    for options in ({}, {"recover": True}, {"recover": True, "preview": True}):
        with pytest.raises(ConfigurationError, match="item-0.*item-1"):
            run(board, project, **options)
    assert path.read_bytes() == before
    assert project.writes == []


def test_lost_legacy_state_never_adopts_an_unmarked_match(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Shared\n")
    project = Project()
    item_id = project.create_draft_issue("project", "Shared")
    project.writes.clear()
    for options in ({}, {"recover": True}):
        with pytest.raises(ConfigurationError, match=item_id):
            run(board, project, **options)
    assert project.writes == []
    assert not (tmp_path / ".kanban.json").exists()


@pytest.mark.parametrize("damage", [
    "foreign marker", "removed marker", "malformed marker", "conflicting task",
    "quoted marker", "missing item", "wrong project", "duplicate local",
])
def test_conflicts_block_an_earlier_create(tmp_path, damage):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    project = Project()
    run(board, project)
    board_at(board, "## TODO\n* [ ] New\n* [ ] Original\n")
    path = tmp_path / ".kanban.json"
    item = project.items[0]
    if damage == "foreign marker":
        item["body"] = item["body"].replace(KEY, "b" * 32)
    elif damage == "removed marker":
        item["body"] = ""
    elif damage == "malformed marker":
        item["body"] = "<!-- kanbanger:item:v1 broken -->"
    elif damage == "quoted marker":
        item["body"] = "Reference to another draft: " + item["body"]
    elif damage == "conflicting task":
        state = json.loads(path.read_text())
        state["tasks"]["Something else"] = state["tasks"].pop("Original")
        path.write_text(json.dumps(state))
    elif damage == "missing item":
        project.items.clear()
    elif damage == "wrong project":
        state = json.loads(path.read_text())
        state["project_id"] = "elsewhere"
        path.write_text(json.dumps(state))
    else:
        board_at(board, "## TODO\n* [ ] New\n* [ ] Original\n## DONE\n* [ ] New\n")
    project.writes.clear()
    before = path.read_bytes()
    for options in ({}, {"recover": True, "preview": True}, {"recover": True}):
        with pytest.raises(ConfigurationError, match="Ownership conflict"):
            run(board, project, **options)
    assert project.writes == []
    assert path.read_bytes() == before


def test_create_response_lost_with_valid_sidecar(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    project = Project()
    board_at(board, "## TODO\n")
    run(board, project)
    board_at(board, "## TODO\n* [ ] Retry\n")
    create = project.create_draft_issue

    def response_lost(*args):
        create(*args)
        raise GitHubAPIError("timeout after create")

    monkeypatch.setattr(project, "create_draft_issue", response_lost)
    with pytest.raises(GitHubAPIError, match="timeout"):
        run(board, project)
    with pytest.raises(ConfigurationError, match="recover"):
        run(board, project)
    assert run(board, project, recover=True, preview=True)["owned"] == {"Retry": "item-0"}
    run(board, project, recover=True)
    run(board, project)
    assert project.writes == [("create", "item-0"), ("update", "item-0")]


def test_retry_after_archive_response_is_lost(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    project = Project()
    board_at(board, "## TODO\n* [ ] Removed\n")
    run(board, project)
    board_at(board, "## TODO\n")
    archive = project.archive_item

    def response_lost(*args):
        archive(*args)
        raise GitHubAPIError("timeout after archive")

    monkeypatch.setattr(project, "archive_item", response_lost)
    with pytest.raises(GitHubAPIError, match="timeout"):
        run(board, project)
    run(board, project)
    assert json.loads((tmp_path / ".kanban.json").read_text())["tasks"] == {}
    assert project.writes.count(("archive", "item-0")) == 1


def test_foreign_marker_with_same_title_is_independent(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Shared\n")
    project = Project()
    run(board, project)
    foreign = project.items[0]
    foreign["body"] = foreign["body"].replace(KEY, "b" * 32)
    (tmp_path / ".kanban.json").unlink()
    project.writes.clear()
    run(board, project)
    assert project.writes == [("create", "item-1"), ("update", "item-1")]


@pytest.mark.parametrize("last_page", [
    {"nodes": [None], "pageInfo": {"hasNextPage": False}},
    {"nodes": [{"id": "hidden", "content": None}], "pageInfo": {"hasNextPage": False}},
    {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "again"}},
    {"nodes": None, "pageInfo": {"hasNextPage": False}},
])
def test_incomplete_inventory_never_starts_a_mutation(tmp_path, monkeypatch, last_page):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] New\n")
    client = GitHubClient("unused")
    monkeypatch.setattr(client, "get_repo_project", Project().get_repo_project)
    calls = []

    def query(document, variables):
        assert "mutation" not in document
        calls.append(variables["cursor"])
        page = last_page if variables["cursor"] else {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "again"}
        }
        return {"data": {"node": {"items": page}}}

    monkeypatch.setattr(client, "_query", query)
    with pytest.raises(GitHubAPIError, match="incomplete project inventory"):
        run(board, client)
    assert calls == [None, "again"]
    assert not (tmp_path / ".kanban.json").exists()


def test_cli_online_preview_outputs_json_and_leaves_no_files(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Proposed\n")
    # Run the installed entry point with a simulated project in a fresh process.
    code = (
        "import sync_kanban as s; "
        "s.GitHubClient.get_repo_project=lambda *a: ('R','P','F',{}); "
        "s.GitHubClient.get_project_items=lambda *a: []; "
        "raise SystemExit(s.main())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(board), "--repo", "owner/repo", "--recover", "--dry-run"],
        cwd=tmp_path, env=dict(os.environ, GITHUB_TOKEN="unused"),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert preview["state"] == "missing" and preview["board_key"] == KEY
    assert preview["plan"]["operations"][0]["title"] == "Proposed"
    assert list(tmp_path.iterdir()) == [board]


@pytest.mark.parametrize("preview", [True, False])
def test_mcp_returns_structured_remote_recovery_report(tmp_path, monkeypatch, preview):
    from kanbanger.tools import register_tools
    from tests.conftest import _StubMCPServer

    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n")
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "unused")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    report = {"owned": {}, "unmarked": {"id": "Unclaimed"}, "state": "missing"}

    class Child:
        def __init__(self, cmd, **options):
            assert cmd == [sys.executable, "-m", "sync_kanban", str(board),
                           "--recover"] + (["--dry-run"] if preview else [])
            assert options["cwd"] == str(tmp_path)
            self.stdout = StringIO(json.dumps(report))
            self.stderr = StringIO()

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(subprocess, "Popen", Child)
    server = _StubMCPServer()
    register_tools(server)
    result = json.loads(server.tools["sync_to_github"](dry_run=preview, recover=True))
    assert result == dict(report, success=True, mode="recovery_preview" if preview else "recovered")


def test_recovery_refuses_conflicting_sidecar_ids(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n")
    path = tmp_path / ".kanban.json"
    record = {"item_id": "shared-id", "status": None}
    path.write_text(json.dumps({"tasks": {"A": record, "B": record}}))
    before = path.read_bytes()
    with pytest.raises(ConfigurationError, match="multiple tasks claim item shared-id"):
        run(board, None, recover=True)
    assert path.read_bytes() == before


def test_concurrent_first_sync_creates_once(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] New\n")
    project = Project()
    at_create = Event()
    second_started = Event()
    create = project.create_draft_issue

    def wait_for_second(*args):
        at_create.set()
        assert second_started.wait(5)
        return create(*args)

    def second_sync():
        second_started.set()
        run(board, project)

    monkeypatch.setattr(project, "create_draft_issue", wait_for_second)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run, board, project)
        assert at_create.wait(5)
        second = pool.submit(second_sync)
        for result in (first, second):
            result.result(timeout=10)
    assert project.writes == [("create", "item-0"), ("update", "item-0")]
