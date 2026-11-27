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


class Project(GitHubClient):
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

    def _query(self, document, variables):
        if "updateProjectV2DraftIssue" in document:
            draft_id = variables["id"]
            item = next(item for item in self.items if item["draft_id"] == draft_id)
            item["body"] = variables["body"]
            self.writes.append(("mark", item["id"]))
            return {"data": {"updateProjectV2DraftIssue": {"draftIssue": {"id": draft_id}}}}
        assert "node(id: $id)" in document
        item = next(item for item in self.items if item["id"] == variables["id"])
        return {"data": {"node": {"id": item["id"], "isArchived": item["archived"],
                                  "content": {"__typename": "DraftIssue", "id": item["draft_id"],
                                              "title": item["title"], "body": item["body"]}}}}


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
    # The server created a draft, but its ID never reached the sidecar.
    for options in ({}, {"recover": True, "preview": True}, {"recover": True}):
        with pytest.raises(ConfigurationError, match="item-0.*unbound marker"):
            run(board, project, **options)
    assert project.writes == [("create", "item-0")]


def test_interrupted_marker_write_retries_using_confirmed_id(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Retry\n")
    project = Project()

    def interrupted(*args):
        raise GitHubAPIError("marker write interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(project, "mark_draft", interrupted)
        with pytest.raises(GitHubAPIError, match="interrupted"):
            run(board, project)
    path = tmp_path / ".kanban.json"
    assert json.loads(path.read_text())["tasks"]["Retry"]["item_id"] == "item-0"
    preview = run(board, project, recover=True, preview=True)
    assert preview["mark_on_next_sync"] == {"Retry": "item-0"}
    run(board, project)
    path.unlink()
    assert run(board, project, recover=True, preview=True)["owned"] == {"Retry": "item-0"}
    assert project.writes == [("create", "item-0"), ("mark", "item-0"), ("update", "item-0")]


def test_v1_marker_needs_matching_sidecar_to_upgrade(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Legacy\n")
    project = Project()
    notes = "Edited notes\r\n\r\n"
    old = notes + f"<!-- kanbanger:item:v1 {KEY} 4c6567616379 -->\n"
    item_id = project.create_draft_issue("project", "Renamed", old)
    with pytest.raises(ConfigurationError, match="unbound marker"):
        run(board, project, recover=True)
    assert project.items[0]["body"] == old
    path = tmp_path / ".kanban.json"
    path.write_text(json.dumps({"tasks": {"Legacy": {"item_id": item_id, "status": "Todo",
                                                     "marked": True}}}))
    run(board, project)
    assert project.items[0]["body"] == notes + f"<!-- kanbanger:item:v2 {KEY} 4c6567616379 {item_id} -->\n"
    path.unlink()
    assert run(board, project, recover=True, preview=True)["owned"] == {"Legacy": item_id}


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
    assert project.writes == [("create", "item-1"), ("mark", "item-1"), ("update", "item-1")]


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
    assert project.writes == [("create", "item-0"), ("mark", "item-0"), ("update", "item-0")]


def test_copy_of_archived_draft_is_never_recovered(tmp_path):
    board = tmp_path / "_kanban.md"
    path = tmp_path / ".kanban.json"
    project = Project()
    board_at(board, "## TODO\n* [ ] Removed\n")
    run(board, project)
    original, = project.items
    project.create_draft_issue("project", "Independent copy", original["body"])
    project.archive_item("project", original["id"])
    before = deepcopy(project.items)
    project.writes.clear()
    board_at(board, "## TODO\n")
    path.unlink()

    for options in ({"recover": True, "preview": True}, {"recover": True}, {}):
        with pytest.raises(ConfigurationError, match="item-1"):
            run(board, project, **options)
    assert project.items == before and project.writes == []
    assert not path.exists()


def test_converted_issue_is_detected_with_lost_state(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    path = tmp_path / ".kanban.json"
    project = Project()
    board_at(board, "## TODO\n* [ ] Converted\n")
    run(board, project)
    original, = project.items
    path.unlink()
    client = GitHubClient("unused")
    monkeypatch.setattr(client, "get_repo_project", project.get_repo_project)

    def query(document, variables):
        assert "mutation" not in document
        content = {"__typename": "Issue"}
        if "... on Issue" in document:
            content.update(id="repo-issue", title="Renamed", body=original["body"])
        page = {"nodes": [{"id": original["id"], "isArchived": False,
                           "content": content, "fieldValueByName": None}],
                "pageInfo": {"hasNextPage": False}}
        return {"data": {"node": {"items": page}}}

    monkeypatch.setattr(client, "_query", query)
    for options in ({"recover": True, "preview": True}, {"recover": True}, {}):
        with pytest.raises(ConfigurationError, match="(?i)item-0.*issue"):
            run(board, client, **options)
    assert not path.exists()


@pytest.mark.parametrize("invalid_first", [True, False])
def test_conflicting_ids_in_malformed_state_refuse_rebuild(tmp_path, invalid_first):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n")
    path = tmp_path / ".kanban.json"
    tasks = {"A": {"item_id": "duplicate", "status": None},
             "B": {"item_id": "duplicate", "status": None},
             "C": {"item_id": "third", "status": 42}}
    if invalid_first:
        tasks = dict(reversed(tasks.items()))
    path.write_text(json.dumps({"tasks": tasks}))
    before = path.read_bytes()
    for preview in (True, False):
        with pytest.raises(ConfigurationError, match="multiple tasks claim item duplicate"):
            run(board, Project(), recover=True, preview=preview)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".kanban.json.corrupt-*"))


def test_corrupt_state_cannot_discard_a_readable_item_claim(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n")
    path = tmp_path / ".kanban.json"
    path.write_text(json.dumps({"tasks": {"Old": {"item_id": "known-id", "status": []}}}))
    before = path.read_bytes()
    with pytest.raises(ConfigurationError, match="known-id"):
        run(board, Project(), recover=True)
    assert path.read_bytes() == before


def test_corrupt_status_recovered_from_item_with_matching_bound_marker(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Existing\n")
    project = Project()
    run(board, project)
    path = tmp_path / ".kanban.json"
    state = json.loads(path.read_text())
    state["tasks"]["Existing"]["status"] = []
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    project.writes.clear()
    preview = run(board, project, recover=True, preview=True)
    assert preview["state"] == "corrupt" and preview["owned"] == {"Existing": "item-0"}
    assert preview["plan"]["operations"] == []
    run(board, project, recover=True)
    backup, = tmp_path.glob(".kanban.json.corrupt-*")
    assert backup.read_bytes() == before
    assert json.loads(path.read_text())["tasks"]["Existing"]["status"] == "Todo"
    assert project.writes == []


def test_cr_only_legacy_board_keeps_present_task_and_line_endings(tmp_path):
    board = tmp_path / "_kanban.md"
    source = b"# Board\r## TODO\r* [ ] Present\r"
    board.write_bytes(source)
    project = Project()
    item_id = project.create_draft_issue("project", "Present")
    (tmp_path / ".kanban.json").write_text(json.dumps({"tasks": {
        "Present": {"item_id": item_id, "status": "Todo"},
    }}))
    assert run(board, project, recover=True, preview=True)["plan"]["summary"]["archive"] == 0
    run(board, project)
    assert not project.items[0]["archived"]
    data = board.read_bytes()
    lines = data.split(b"\r")
    assert lines.pop(1).startswith(b"<!-- kanbanger:board-id:")
    assert b"\r".join(lines) == source
    assert b"\n" not in data
    assert run(board, project, recover=True, preview=True)["owned"] == {"Present": item_id}


def test_marker_insertion_preserves_edit_during_inventory(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    source = b"# Board\r\n## TODO\r\n"
    edited = source + b"* [ ] Added during sync\r\n"
    board.write_bytes(source)
    project = Project()

    def edit_board(project_id):
        board.write_bytes(edited)
        return []

    monkeypatch.setattr(project, "get_project_items", edit_board)
    with pytest.raises(ConfigurationError, match="(?i)board.*changed"):
        run(board, project, recover=True)
    assert board.read_bytes() == edited
    assert not (tmp_path / ".kanban.json").exists()
    assert project.writes == []


def test_legacy_migration_preserves_edit_during_inventory(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Legacy\n")
    project = Project()
    item_id = project.create_draft_issue("project", "Legacy", "Notes")
    path = tmp_path / ".kanban.json"
    path.write_text(json.dumps({"tasks": {"Legacy": {"item_id": item_id, "status": "Todo"}}}))
    before = path.read_bytes()
    project.writes.clear()
    inventory = project.get_project_items

    def edit_draft(project_id):
        snapshot = inventory(project_id)
        project.items[0]["body"] = "Notes\n\nNew paragraph"
        return snapshot

    monkeypatch.setattr(project, "get_project_items", edit_draft)
    with pytest.raises(ConfigurationError, match="(?i)item-0.*changed"):
        run(board, project)
    assert project.items[0]["body"] == "Notes\n\nNew paragraph"
    assert path.read_bytes() == before and project.writes == []


def test_lost_binding_refuses_first_of_two_projects(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Existing in second project\n")
    client = GitHubClient("unused")
    projects = [{"id": str(n), "number": n, "title": str(n),
                 "fields": {"nodes": [{"id": "field", "name": "Status", "options": []}]}}
                for n in (11, 8)]

    def query(document, variables):
        assert "repository(owner" in document
        page = int(variables["cursor"] is not None)
        return {"data": {"repository": {"id": "repo", "projectsV2": {
            "nodes": [projects[page]], "pageInfo": {"hasNextPage": not page, "endCursor": "next"},
        }}}}

    monkeypatch.setattr(client, "_query", query)
    syncer = Syncer(LocalBoard(str(board)), StateManager(str(board)), client)
    for options in ({}, {"recover": True}, {"recover": True, "preview": True}):
        with pytest.raises(ConfigurationError, match="GITHUB_PROJECT_NUMBER"):
            syncer.sync("owner/repo", **options)
    assert not (tmp_path / ".kanban.json").exists()
    assert client.get_repo_project("owner", "repo", 8)[1] == "8"
    path = tmp_path / ".kanban.json"
    path.write_text(json.dumps({"project_id": "8", "tasks": {}}))
    monkeypatch.setattr(client, "get_project_items", lambda project_id: [])
    assert syncer.sync("owner/repo", recover=True, preview=True)["project_id"] == "8"
    path.write_text(json.dumps({"project_id": "8", "tasks": []}))
    with pytest.raises(ConfigurationError, match="GITHUB_PROJECT_NUMBER"):
        syncer.sync("owner/repo", recover=True, preview=True)


def test_board_edit_during_atomic_replace_retry_survives(tmp_path, monkeypatch):
    import kanban_io

    board = tmp_path / "_kanban.md"
    board.write_bytes(b"# Board\n")
    edited = b"# Board\n## TODO\n* [ ] New\n"
    replace = os.replace
    attempts = []

    def busy_file(source, target):
        if not attempts:
            attempts.append(target)
            monkeypatch.setattr(kanban_io.sys, "platform", "win32")
            board.write_bytes(edited)
            raise PermissionError("editor was saving")
        replace(source, target)

    monkeypatch.setattr(os, "replace", busy_file)
    with pytest.raises(ConfigurationError, match="(?i)board.*changed"):
        run(board, Project(), recover=True)
    assert board.read_bytes() == edited
    assert not (tmp_path / ".kanban.json").exists()
    assert not list(tmp_path.glob("_kanban.md.tmp.*"))
