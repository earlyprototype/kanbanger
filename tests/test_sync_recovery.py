"""Exercise ownership recovery with a stateful GitHub Project double."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
import os
from pathlib import Path
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
        self.provenance_field_id = None

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
        if "createProjectV2Field" in document:
            return {"data": {"createProjectV2Field": {"projectV2Field": {"id": "provenance-field"}}}}
        if "updateProjectV2ItemFieldValue" in document:
            item = next(item for item in self.items if item["id"] == variables["id"])
            item["provenance"] = variables["marker"]
            self.writes.append(("mark", item["id"]))
            return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item["id"]}}}}
        assert "node(id: $id)" in document
        item = next(item for item in self.items if item["id"] == variables["id"])
        return {"data": {"node": {"id": item["id"], "isArchived": item["archived"],
                                  "provenance": {"text": item["provenance"]} if "provenance" in item else None,
                                  "content": {"__typename": item.get("type", "DraftIssue"), "id": item["draft_id"],
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
    assert preview["plan"]["summary"] == {
        "create": 0, "update": 0, "archive": 0, "pending_update": 1, "pending_archive": 1,
    }
    assert [(op["action"], op["item_id"]) for op in preview["plan"]["operations"]] == [
        ("PENDING_UPDATE", first["id"]), ("PENDING_ARCHIVE", removed["id"]),
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
    assert project.writes == []
    assert first["status"] == "Todo"
    assert not removed["archived"]
    assert json.loads(path.read_bytes())["tasks"]["Removed"]["item_id"] == removed["id"]
    # The operator completes both pending actions on GitHub.
    project.update_item_status("project", first["id"], "status", "Done")
    project.archive_item("project", removed["id"])
    run(board, project)
    tasks = json.loads(path.read_bytes())["tasks"]
    assert "Removed" not in tasks
    assert tasks["Café - review"]["status"] == "Done"
    assert tasks["Café - review"]["recovered"]
    assert project.writes == [("update", first["id"]), ("archive", removed["id"])]
    assert project.items[0] == unrelated


def test_duplicate_markers_refuse_the_entire_plan(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] One\n")
    project = Project()
    run(board, project)
    project.create_draft_issue("project", "Different title", project.items[0]["provenance"])
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


def test_converted_pre_marker_draft_blocks_create_after_state_loss(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Converted\n")
    project = Project()
    item_id = project.create_draft_issue("project", "Converted", "Legacy notes")
    state = tmp_path / ".kanban.json"
    state.write_text(json.dumps({"tasks": {"Converted": {"item_id": item_id, "status": "Todo"}}}))
    project.items[0].update(type="Issue", draft_id=None)
    state.unlink()
    project.writes.clear()
    before = deepcopy(project.items)

    for options in ({"recover": True, "preview": True}, {"recover": True}, {}):
        with pytest.raises(ConfigurationError, match=f"{item_id}.*Converted"):
            run(board, project, **options)
    assert project.items == before
    assert project.writes == [] and not state.exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_renamed_converted_legacy_draft_requires_explicit_recovery(tmp_path, corrupt):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    project = Project()
    item_id = project.create_draft_issue("project", "Original", "Legacy notes")
    state = tmp_path / ".kanban.json"
    state.write_text(json.dumps({"tasks": {"Original": {"item_id": item_id, "status": "Todo"}}}))
    project.items[0].update(type="Issue", draft_id=None)
    project.items[0]["title"] = "Renamed after conversion"
    state.unlink()
    if corrupt:
        state.write_bytes(b"{")
    project.writes.clear()
    before = deepcopy(project.items)

    preview = run(board, project, recover=True, preview=True)
    assert preview["unmarked"] == {item_id: "Renamed after conversion"}
    assert preview["owned"] == {}
    assert preview["plan"]["operations"][0]["action"] == "CREATE"
    with pytest.raises(ConfigurationError, match="recover"):
        run(board, project)
    if corrupt:
        assert state.read_bytes() == b"{"
    else:
        assert not state.exists()
    assert project.writes == [] and project.items == before
    # The operator can remove the local task after identifying the issue.
    board_at(board, "## TODO\n")
    run(board, project, recover=True)
    run(board, project)
    assert project.writes == [] and project.items == before
    assert json.loads(state.read_bytes())["tasks"] == {}


def test_stale_backup_requires_review_of_each_unmarked_item_before_create(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    path = tmp_path / ".kanban.json"
    backup = b'{"tasks":{}}'
    path.write_bytes(backup)
    project = Project()
    converted = project.create_draft_issue("project", "Original", "Legacy notes")
    path.write_text(json.dumps({"tasks": {"Original": {"item_id": converted, "status": "Todo"}}}))
    project.items[0].update(type="Issue", draft_id=None, title="Renamed after conversion")
    project.writes.clear()

    with pytest.raises(ConfigurationError, match=f"{converted}.*repository issue"):
        run(board, project)
    path.write_bytes(backup)
    with pytest.raises(ConfigurationError, match="recover"):
        run(board, project)
    preview = run(board, project, recover=True, preview=True)
    assert preview["unmarked"] == {converted: "Renamed after conversion"}
    assert preview["owned"] == {} and preview["plan"]["summary"]["create"] == 1
    assert path.read_bytes() == backup and project.writes == []

    # Remove the local duplicate. A different ID arrives after the preview.
    board_at(board, "## TODO\n* [ ] New work\n")
    token = run(board, project, recover=True, preview=True)["review_token"]
    other = project.create_draft_issue("project", "Renamed after conversion")
    sentinels = deepcopy(project.items)
    project.writes.clear()
    with pytest.raises(ConfigurationError, match="current preview"):
        run(board, project, recover=True, reviewed_item_ids=[converted], review_token=token)
    token = run(board, project, recover=True, preview=True)["review_token"]
    report = run(board, project, recover=True, reviewed_item_ids=[converted], review_token=token)
    assert report["unreviewed_item_ids"] == [other]
    assert set(json.loads(path.read_bytes())["reviewed_unmarked_items"]) == {converted}
    with pytest.raises(ConfigurationError, match=other):
        run(board, project)
    assert project.writes == []
    preview = run(board, project, recover=True, preview=True)
    assert preview["unmarked"] == {converted: "Renamed after conversion", other: "Renamed after conversion"}
    assert preview["unreviewed_item_ids"] == [other]
    with pytest.raises(ConfigurationError, match="absent-id"):
        run(board, project, recover=True, reviewed_item_ids=[converted, other, "absent-id"], review_token=token)
    run(board, project, recover=True, reviewed_item_ids=[other], review_token=token)
    assert set(json.loads(path.read_bytes())["reviewed_unmarked_items"]) == {converted, other}
    run(board, project)
    assert project.items[:2] == sentinels
    assert project.writes == [("create", "item-2"), ("mark", "item-2"), ("update", "item-2")]

    # Ordinary sync must check even an already reviewed sidecar on the next CREATE.
    later = project.create_draft_issue("project", "Renamed after conversion")
    project.writes.clear()
    board_at(board, "## TODO\n* [ ] New work\n* [ ] Another task\n")
    with pytest.raises(ConfigurationError, match=later):
        run(board, project)
    assert project.writes == []


@pytest.mark.parametrize("notes", [
    "Look for the prefix `<!-- kanbanger:item:` in the body.",
    "<!-- kanbanger:item:... is the start of a marker in a draft.",
])
def test_documentation_issue_can_mention_the_marker_prefix(tmp_path, notes):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] New\n")
    project = Project()
    project.create_draft_issue("project", "Documentation", notes)
    project.items[0].update(type="Issue", draft_id=None)
    sentinel = deepcopy(project.items[0])
    project.writes.clear()
    preview = run(board, project, recover=True, preview=True)
    assert preview["plan"]["summary"] == {"create": 1, "update": 0, "archive": 0}
    assert preview["unmarked"] == {"item-0": "Documentation"}
    run(board, project, recover=True, reviewed_item_ids=list(preview["unmarked"]),
        review_token=preview["review_token"])
    run(board, project)
    assert project.items[0] == sentinel
    assert project.writes == [("create", "item-1"), ("mark", "item-1"), ("update", "item-1")]


@pytest.mark.parametrize("initial", [None, b"{", b'{"tasks":{}}'])
def test_sidecar_restored_during_scan_keeps_renamed_legacy_claim(tmp_path, monkeypatch, initial):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    project = Project()
    item_id = project.create_draft_issue("project", "Renamed")
    restored = json.dumps({"tasks": {"Original": {"item_id": item_id, "status": "Todo"}}}).encode()
    path = tmp_path / ".kanban.json"
    if initial is not None:
        path.write_bytes(initial)
    project.writes.clear()
    inventory = project.get_project_items

    def restore_sidecar(project_id):
        items = inventory(project_id)
        path.write_bytes(restored)
        return items

    with monkeypatch.context() as patch:
        patch.setattr(project, "get_project_items", restore_sidecar)
        with pytest.raises(ConfigurationError, match="(?i)(sidecar|state).*changed"):
            run(board, project, recover=True)
    assert path.read_bytes() == restored and not list(tmp_path.glob(".kanban.json.corrupt-*"))
    with pytest.raises(ConfigurationError, match=item_id):
        run(board, project)
    assert project.writes == []


def test_recovery_detects_sidecar_removal_during_scan(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n")
    path = tmp_path / ".kanban.json"
    path.write_bytes(b'{"tasks":{}}')
    project = Project()

    def remove_sidecar(project_id):
        path.unlink()
        return []

    monkeypatch.setattr(project, "get_project_items", remove_sidecar)
    with pytest.raises(ConfigurationError, match="(?i)(sidecar|state).*changed"):
        run(board, project, recover=True)
    assert not path.exists()


@pytest.mark.parametrize("stage", ["create", "initial marker", "initial status", "update", "archive"])
def test_restore_during_ordinary_write_preserves_bytes_and_reports_item(tmp_path, monkeypatch, stage):
    board = tmp_path / "_kanban.md"
    path = tmp_path / ".kanban.json"
    project = Project()
    board_at(board, "## TODO\n* [ ] Current\n")
    if stage in ("update", "archive"):
        run(board, project)
        board_at(board, "## DONE\n* [ ] Current\n" if stage == "update" else "## TODO\n")
    else:
        path.write_bytes(b'{"tasks":{}}')
    legacy = project.create_draft_issue("project", "Legacy", "Existing draft")
    if stage in ("create", "initial marker", "initial status"):
        token = run(board, project, recover=True, preview=True)["review_token"]
        run(board, project, recover=True, reviewed_item_ids=[legacy], review_token=token)
    restored = json.dumps({"tasks": {"Legacy": {"item_id": legacy, "status": "Todo"}}},
                          indent=4).encode() + b"\n"
    methods = {"create": "create_draft_issue", "archive": "archive_item", "initial marker": "mark_draft"}
    method = methods.get(stage, "update_item_status")
    mutate = getattr(project, method)

    def restore_in_flight(*args):
        result = mutate(*args)
        path.write_bytes(restored)
        return result

    project.writes.clear()
    monkeypatch.setattr(project, method, restore_in_flight)
    with pytest.raises(ConfigurationError) as raised:
        run(board, project)
    error = str(raised.value)
    operation, item_id = project.writes[-1]
    assert "changed" in error and operation.upper() in error and item_id in error
    assert "Current" in error and path.read_bytes() == restored
    assert not list(tmp_path.glob(".kanban.json.*"))


def test_recovery_checks_the_displaced_sidecar(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n")
    path = tmp_path / ".kanban.json"
    path.write_bytes(b"{")
    restored = b'{"tasks":{"Legacy":{"item_id":"legacy-id","status":"Todo"}}}'
    rename = Path.rename

    def restore_before_rename(source, target):
        if source == path:
            path.write_bytes(restored)
        return rename(source, target)

    monkeypatch.setattr(Path, "rename", restore_before_rename)
    with pytest.raises(ConfigurationError, match="state changed"):
        run(board, Project(), recover=True)
    assert path.read_bytes() == restored
    assert not list(tmp_path.glob(".kanban.json.*"))


@pytest.mark.parametrize("column", ["REVIEW", "TODO"])
def test_restore_during_marker_binding_stops_before_next_write(tmp_path, monkeypatch, column):
    board = tmp_path / "_kanban.md"
    board_at(board, f"## {column}\n* [ ] First\n* [ ] Second\n")
    path = tmp_path / ".kanban.json"
    project = Project()
    mark = project.mark_draft
    restored = b'{"tasks":{}}'

    def restore_during_mark(*args):
        mark(*args)
        path.write_bytes(restored)

    monkeypatch.setattr(project, "mark_draft", restore_during_mark)
    with pytest.raises(ConfigurationError, match=r"after MARK of 'First' \(item-0\)"):
        run(board, project)
    assert project.writes == [("create", "item-0"), ("mark", "item-0")]
    assert path.read_bytes() == restored


@pytest.mark.parametrize(("initial", "recover"), [
    (None, True), (b"{", True), (None, False), (b'{"tasks":{}}', False),
])
def test_sync_never_replaces_a_restore_at_publication(tmp_path, monkeypatch, initial, recover):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n")
    path = tmp_path / ".kanban.json"
    if initial is not None:
        path.write_bytes(initial)
    restored = b'{"tasks":{"Legacy":{"item_id":"legacy-id","status":"Todo"}}}'
    install = StateManager._install_if_absent

    def restore_before_install(source, target):
        if not path.exists():
            path.write_bytes(restored)
        install(source, target)

    monkeypatch.setattr(StateManager, "_install_if_absent", staticmethod(restore_before_install))
    with pytest.raises(ConfigurationError, match="state changed"):
        run(board, Project(), recover=recover)
    assert path.read_bytes() == restored
    displaced = tmp_path / ".kanban.json.recovering"
    if initial is not None:
        assert displaced.read_bytes() == initial
        with pytest.raises(ConfigurationError, match="Interrupted recovery"):
            run(board, None)
    else:
        assert not displaced.exists()


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
        del item["provenance"]
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


def test_interrupted_marker_write_resumes_after_operator_binds_confirmed_id(tmp_path, monkeypatch):
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
    with pytest.raises(ConfigurationError, match="binding pending for item-0"):
        run(board, project)
    assert project.writes == [("create", "item-0")]
    project.items[0]["body"] = preview["pending_legacy_bindings"]["item-0"]
    run(board, project)
    path.unlink()
    assert run(board, project, recover=True, preview=True)["owned"] == {"Retry": "item-0"}
    assert project.writes == [("create", "item-0"), ("update", "item-0")]


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
    path.write_text(json.dumps({"board_key": KEY, "tasks": {"Legacy": {"item_id": item_id, "status": "Todo",
                                                     "marked": True}}}))
    with pytest.raises(ConfigurationError, match="binding pending"):
        run(board, project)
    assert project.items[0]["body"] == old
    marker = run(board, project, recover=True, preview=True)["pending_legacy_bindings"][item_id]
    assert marker == f"<!-- kanbanger:item:v2 {KEY} 4c6567616379 {item_id} -->"
    project.items[0]["body"] = notes + marker
    run(board, project)
    assert project.items[0]["body"] == notes + marker
    path.unlink()
    assert run(board, project, recover=True, preview=True)["owned"] == {"Legacy": item_id}


def test_recorded_legacy_notes_can_quote_a_marker_prefix(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Legacy\n")
    project = Project()
    notes = "Keep this example: `<!-- kanbanger:item:`.\r\n"
    item_id = project.create_draft_issue("project", "Legacy", notes)
    path = tmp_path / ".kanban.json"
    path.write_text(json.dumps({"board_key": KEY, "tasks": {"Legacy": {"item_id": item_id, "status": "Todo"}}}))
    preview = run(board, project, recover=True, preview=True)
    assert preview["pending_legacy_bindings"] == {item_id: f"<!-- kanbanger:item:v2 {KEY} 4c6567616379 {item_id} -->"}
    with pytest.raises(ConfigurationError, match="binding pending"):
        run(board, project)
    assert project.items[0]["body"] == notes
    project.items[0]["body"] += preview["pending_legacy_bindings"][item_id]
    run(board, project)
    project.items[0]["body"] = notes
    before = path.read_bytes()
    project.writes.clear()
    with pytest.raises(ConfigurationError, match=f"{item_id}.*marker missing"):
        run(board, project)
    assert project.writes == [] and path.read_bytes() == before


def test_recorded_legacy_draft_with_a_malformed_marker_line_refuses_migration(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Legacy\n")
    project = Project()
    body = "Notes\n\n<!-- kanbanger:item:broken -->"
    item_id = project.create_draft_issue("project", "Legacy", body)
    path = tmp_path / ".kanban.json"
    path.write_text(json.dumps({"tasks": {"Legacy": {"item_id": item_id, "status": "Todo"}}}))
    before = path.read_bytes()
    project.writes.clear()
    with pytest.raises(ConfigurationError, match=f"{item_id}.*invalid.*marker"):
        run(board, project)
    assert project.items[0]["body"] == body and project.writes == []
    assert path.read_bytes() == before


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
    for field in ("body", "provenance"):
        foreign[field] = foreign[field].replace(KEY, "b" * 32)
    (tmp_path / ".kanban.json").unlink()
    project.writes.clear()
    run(board, project)
    assert project.writes == [("create", "item-1"), ("mark", "item-1"), ("update", "item-1")]


@pytest.mark.parametrize("bound", [True, False])
def test_foreign_marker_copied_to_renamed_converted_legacy_draft(tmp_path, bound):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    project = Project()
    foreign = project.create_draft_issue("project", "Other board's draft")
    owner = "b" * 32
    marker = (f"<!-- kanbanger:item:v2 {owner} 4f74686572 {foreign} -->" if bound
              else f"<!-- kanbanger:item:v1 {owner} 4f74686572 -->")
    project.items[0]["body"] = marker
    converted = project.create_draft_issue("project", "Original", "Legacy notes")
    project.items[1].update(type="Issue", title="Renamed issue", draft_id=None, body=marker)
    before = deepcopy(project.items)
    project.writes.clear()

    for options in ({"recover": True, "preview": True}, {"recover": True}, {}):
        message = rf"{foreign}.*{converted}" if bound else rf"{converted}.*unbound"
        with pytest.raises(ConfigurationError, match=message):
            run(board, project, **options)
    assert project.items == before
    assert project.writes == [] and not (tmp_path / ".kanban.json").exists()


@pytest.mark.parametrize("sync_reviewed_create", [False, True])
def test_restoring_task_requires_fresh_review_for_renamed_issue(tmp_path, sync_reviewed_create):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] New work\n")
    project = Project()
    converted = project.create_draft_issue("project", "Original", "Legacy notes")
    project.items[0].update(type="Issue", title="Renamed issue", draft_id=None)
    issue = deepcopy(project.items[0])
    project.writes.clear()
    preview = run(board, project, recover=True, preview=True)
    assert [op["title"] for op in preview["plan"]["operations"]] == ["New work"]
    with pytest.raises(ConfigurationError, match="review-token"):
        run(board, project, recover=True, reviewed_item_ids=[converted])
    run(board, project, recover=True, reviewed_item_ids=[converted], review_token=preview["review_token"])
    if sync_reviewed_create:
        run(board, project)
        assert [item["title"] for item in project.items] == ["Renamed issue", "New work"]
        assert project.items[0] == issue
        project.writes.clear()
    path = tmp_path / ".kanban.json"
    reviewed = path.read_bytes()
    before = deepcopy(project.items)

    board_at(board, "## TODO\n* [ ] New work\n* [ ] Original\n")
    with pytest.raises(ConfigurationError, match=converted):
        run(board, project)
    assert run(board, project, recover=True, preview=True)["unreviewed_item_ids"] == [converted]
    with pytest.raises(ConfigurationError, match="current preview"):
        run(board, project, recover=True, reviewed_item_ids=[converted], review_token=preview["review_token"])
    assert path.read_bytes() == reviewed and project.items == before
    assert project.writes == []


@pytest.mark.parametrize("old_scope", [{}, {"reviewed_create_titles": ["Restored"]}])
def test_old_item_only_review_cannot_authorize_a_create(tmp_path, old_scope):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Restored\n")
    project = Project()
    converted = project.create_draft_issue("project", "Renamed")
    project.items[0].update(type="Issue", draft_id=None)
    path = tmp_path / ".kanban.json"
    original = json.dumps({"board_key": KEY, "project_id": "project", "tasks": {},
                           "reviewed_unmarked_ids": [converted], **old_scope}).encode()
    path.write_bytes(original)
    project.writes.clear()
    preview = run(board, project, recover=True, preview=True)
    assert preview["unreviewed_item_ids"] == [converted]
    with pytest.raises(ConfigurationError, match=converted):
        run(board, project)
    assert path.read_bytes() == original and project.writes == []


def test_unbound_legacy_claim_requires_board_and_item_confirmation(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n")
    project = Project()
    notes = "Other board's notes\r\n\r\n"
    item_id = project.create_draft_issue("project", "Legacy", notes)
    path = tmp_path / ".kanban.json"
    original = json.dumps({"project_id": "project", "tasks": {
        "Legacy": {"item_id": item_id, "status": "Todo"}
    }}).encode()
    path.write_bytes(original)
    project.writes.clear()
    preview = run(board, project, recover=True, preview=True)
    assert preview["pending_legacy_claims"] == {item_id: "Legacy"}
    assert preview["owned"] == preview["mark_on_next_sync"] == preview["pending_legacy_bindings"] == {}
    assert preview["plan"]["operations"] == []
    for options in ({"recover": True}, {}):
        with pytest.raises(ConfigurationError, match=item_id):
            run(board, project, **options)
    assert path.read_bytes() == original and project.writes == []
    assert project.items[0]["body"].encode() == notes.encode()
    with pytest.raises(ConfigurationError, match="wrong-item"):
        run(board, project, recover=True, review_token=preview["review_token"],
            confirmed_legacy_item_ids=["wrong-item"])
    board.write_text(board.read_text().replace(KEY, "b" * 32))
    with pytest.raises(ConfigurationError, match="current preview"):
        run(board, project, recover=True, review_token=preview["review_token"],
            confirmed_legacy_item_ids=[item_id])
    assert path.read_bytes() == original and project.writes == []


def test_partial_legacy_confirmation_preserves_unbound_sidecar(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Active\n")
    project = Project()
    active = project.create_draft_issue("project", "Active", "Notes")
    archived = project.create_draft_issue("project", "Archived")
    project.archive_item("project", archived)
    original = json.dumps({"tasks": {
        "Active": {"item_id": active, "status": None},
        "Archived": {"item_id": archived, "status": None},
    }}).encode()
    path = tmp_path / ".kanban.json"
    path.write_bytes(original)
    before = deepcopy(project.items)
    project.writes.clear()
    preview = run(board, project, recover=True, preview=True)
    assert preview["pending_legacy_claims"] == {active: "Active", archived: "Archived"}
    token = preview["review_token"]
    with pytest.raises(ConfigurationError, match=archived):
        run(board, project, recover=True, review_token=token, confirmed_legacy_item_ids=[active])
    assert path.read_bytes() == original and project.items == before and project.writes == []

    report = run(board, project, recover=True, review_token=token, confirmed_legacy_item_ids=[active, archived])
    assert report["pending_legacy_claims"] == {}
    assert report["owned"] == {"Active": active} and report["archived"] == {archived: "Archived"}
    assert project.items == before and project.writes == []
    assert report["pending_legacy_bindings"] == {active: f"<!-- kanbanger:item:v2 {KEY} 416374697665 {active} -->"}
    with pytest.raises(ConfigurationError, match=f"binding pending for {active}"):
        run(board, project)
    assert project.items == before and project.writes == []
    project.items[0]["body"] += "\n" + report["pending_legacy_bindings"][active]
    run(board, project)
    assert project.writes == []
    assert project.items[1] == before[1]


@pytest.mark.parametrize("boundary", ["after inventory", "at field write"])
def test_confirmed_legacy_conversion_gets_no_writes(tmp_path, monkeypatch, boundary):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    project = Project()
    item_id = project.create_draft_issue("project", "Original", "Legacy notes")
    item, = project.items
    item["status"] = "Todo"
    path = tmp_path / ".kanban.json"
    path.write_text(json.dumps({"tasks": {"Original": {"item_id": item_id, "status": "Todo"}}}))
    preview = run(board, project, recover=True, preview=True)
    run(board, project, recover=True, confirmed_legacy_item_ids=[item_id],
        review_token=preview["review_token"])
    confirmed = path.read_bytes()
    project.writes.clear()

    def convert():
        item.update(type="Issue", draft_id=None, title="Renamed after conversion")

    inventory = project.get_project_items
    query = project._query

    def snapshot(project_id):
        rows = inventory(project_id)
        if boundary == "after inventory":
            convert()
        return rows

    def convert_at_write(document, variables):
        if "updateProjectV2ItemFieldValue" in document:
            convert()
        return query(document, variables)

    monkeypatch.setattr(project, "get_project_items", snapshot)
    monkeypatch.setattr(project, "_query", convert_at_write)
    error = None
    try:
        run(board, project)
    except ConfigurationError as exc:
        error = str(exc)
    assert project.writes == []
    assert error and item_id in error and "pending" in error.lower()
    assert "provenance" not in item and path.read_bytes() == confirmed

    # A refused field write never reaches the mutation hook. Convert the draft
    # before the next scan in that case and verify the recorded ID still blocks sync.
    convert()
    for options in ({}, {"recover": True, "preview": True}, {"recover": True}):
        with pytest.raises(ConfigurationError, match=f"{item_id}.*repository issue"):
            run(board, project, **options)
    assert project.writes == [] and path.read_bytes() == confirmed


def test_appended_notes_preserve_field_bound_draft_through_recovery(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    project = Project()
    run(board, project)
    item, = project.items
    item["title"] = "Renamed on GitHub"
    item["body"] = "Opening notes\r\n\r\n" + item["body"] + "\r\n\r\nAppended paragraph.\r\n"
    before = deepcopy(item)
    project.writes.clear()
    run(board, project)
    path = tmp_path / ".kanban.json"
    path.unlink()
    preview = run(board, project, recover=True, preview=True)
    assert preview["owned"] == {"Original": item["id"]} and preview["plan"]["operations"] == []
    assert not path.exists()
    run(board, project, recover=True)
    run(board, project)
    assert json.loads(path.read_bytes())["tasks"]["Original"]["item_id"] == item["id"]
    assert project.items == [before] and project.writes == []


@pytest.mark.parametrize("damage", [
    "missing field", "wrong item ID", "duplicate", "malformed", "contradictory", "embedded",
])
def test_appended_notes_with_unverified_claim_block_recovery(tmp_path, damage):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    project = Project()
    run(board, project)
    item, = project.items
    provisional = item["body"]
    if damage == "missing field":
        del item["provenance"]
    elif damage == "wrong item ID":
        item["provenance"] = item["provenance"].replace(item["id"], "other-id")
    elif damage == "duplicate":
        item["body"] += "\n" + provisional
    elif damage == "malformed":
        item["body"] += "\n<!-- kanbanger:item:v1 broken -->"
    elif damage == "contradictory":
        item["body"] = provisional.replace(KEY, "b" * 32)
    else:
        item["body"] = "Quoted " + provisional
    item["body"] += "\n\nAppended paragraph."
    path = tmp_path / ".kanban.json"
    project.writes.clear()
    with pytest.raises(ConfigurationError, match="Ownership conflict"):
        run(board, project)
    path.unlink()
    for preview in (True, False):
        with pytest.raises(ConfigurationError, match="Ownership conflict"):
            run(board, project, recover=True, preview=preview)
    assert project.writes == [] and not path.exists()


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
        return {"data": {"node": {"provenanceField": None, "items": page}}}

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


def test_cli_records_only_explicitly_reviewed_item_ids(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    code = (
        "import sync_kanban as s; "
        "s.GitHubClient.get_repo_project=lambda *a: ('R','P','F',{}); "
        "s.GitHubClient.get_project_items=lambda *a: "
        "[dict(id='issue-id', type='Issue', title='Renamed', body='', archived=False, status=None)]; "
        "raise SystemExit(s.main())"
    )
    command = [sys.executable, "-c", code, str(board), "--repo", "owner/repo"]
    options = dict(cwd=tmp_path, env=dict(os.environ, GITHUB_TOKEN="unused"),
                   capture_output=True, text=True, timeout=30)
    for flags in (["--dry-run"], ["--recover", "--dry-run"], []):
        result = subprocess.run(command + flags + ["--reviewed-item", "issue-id"], **options)
        assert result.returncode == 1 and "--recover" in result.stderr
        assert not (tmp_path / ".kanban.json").exists()
    preview = subprocess.run(command + ["--recover", "--dry-run"], **options)
    token = json.loads(preview.stdout)["review_token"]
    result = subprocess.run(command + ["--recover", "--reviewed-item", "issue-id", "--review-token", token], **options)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["unreviewed_item_ids"] == []
    assert json.loads((tmp_path / ".kanban.json").read_bytes())["reviewed_unmarked_items"] == {
        "issue-id": {"type": "Issue", "title": "Renamed"}
    }


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
                           "--recover"] + (["--dry-run"] if preview else [
                               "--reviewed-item=id", "--confirm-legacy-item=draft", "--review-token=from-preview"])
            assert options["cwd"] == str(tmp_path)
            self.stdout = StringIO(json.dumps(report))
            self.stderr = StringIO()

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(subprocess, "Popen", Child)
    server = _StubMCPServer()
    register_tools(server)
    sync = server.tools["sync_to_github"]
    assert not json.loads(sync(dry_run=True, reviewed_item_ids=["id"]))["success"]
    confirmations = {} if preview else dict(reviewed_item_ids=["id"], confirmed_legacy_item_ids=["draft"],
                                            review_token="from-preview")
    result = json.loads(sync(dry_run=preview, recover=True, **confirmations))
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


@pytest.mark.parametrize("kind", ["Issue", "PullRequest"])
@pytest.mark.parametrize("corrupt", [False, True])
@pytest.mark.parametrize("marker_source", ["bound comment", "provisional comment", "field"])
def test_converted_item_is_detected_with_lost_state(tmp_path, monkeypatch, kind, corrupt, marker_source):
    board = tmp_path / "_kanban.md"
    path = tmp_path / ".kanban.json"
    project = Project()
    board_at(board, "## TODO\n* [ ] Converted\n")
    run(board, project)
    original, = project.items
    path.unlink()
    if corrupt:
        path.write_bytes(b"{")
    client = GitHubClient("unused")
    monkeypatch.setattr(client, "get_repo_project", project.get_repo_project)

    def query(document, variables):
        assert "mutation" not in document
        content = {"__typename": kind}
        if f"... on {kind} {{ id title body }}" in document:
            body = {"bound comment": original["provenance"], "provisional comment": original["body"],
                    "field": "Edited notes"}[marker_source]
            content.update(id="content-id", title="Renamed", body=body)
        provenance = {"text": original["provenance"]} if marker_source == "field" else None
        page = {"nodes": [{"id": original["id"], "isArchived": False,
                           "content": content, "fieldValueByName": None, "provenance": provenance}],
                "pageInfo": {"hasNextPage": False}}
        return {"data": {"node": {"provenanceField": {"id": "field", "dataType": "TEXT"}, "items": page}}}

    monkeypatch.setattr(client, "_query", query)
    for options in ({"recover": True, "preview": True}, {"recover": True}, {}):
        message = "corrupt" if corrupt and not options else r"item-0.*(?:issue|pull request).*Converted"
        with pytest.raises(ConfigurationError, match=message):
            run(board, client, **options)
    assert (path.read_bytes() if path.exists() else None) == (b"{" if corrupt else None)
    assert not list(tmp_path.glob(".kanban.json.corrupt-*"))


@pytest.mark.parametrize("corrupt", [False, True])
def test_unmarked_renamed_pull_request_requires_review(tmp_path, monkeypatch, corrupt):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    path = tmp_path / ".kanban.json"
    if corrupt:
        path.write_bytes(b"{")
    client = GitHubClient("unused")
    monkeypatch.setattr(client, "get_repo_project", Project().get_repo_project)

    def query(document, variables):
        assert "mutation" not in document
        content = {"__typename": "PullRequest", "id": "PR", "title": "Renamed", "body": "notes"}
        page = {"nodes": [{"id": "original-id", "content": content, "isArchived": False,
                           "fieldValueByName": None, "provenance": None}],
                "pageInfo": {"hasNextPage": False}}
        return {"data": {"node": {"provenanceField": None, "items": page}}}

    monkeypatch.setattr(client, "_query", query)
    preview = run(board, client, recover=True, preview=True)
    assert preview["unmarked_items"] == {"original-id": {"type": "PullRequest", "title": "Renamed"}}
    assert preview["unreviewed_item_ids"] == ["original-id"]
    with pytest.raises(ConfigurationError, match="recover"):
        run(board, client)
    run(board, client, recover=True)
    with pytest.raises(ConfigurationError, match="original-id"):
        run(board, client)


@pytest.mark.parametrize("change", [{"type": "Issue"}, {"title": "Renamed"},
                                    {"type": "PullRequest", "title": "Converted and renamed"}])
@pytest.mark.parametrize("stage", ["apply", "sync", "before create"])
def test_review_expires_when_item_evidence_changes(tmp_path, monkeypatch, change, stage):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Original\n")
    project = Project()
    item_id = project.create_draft_issue("project", "Candidate", "notes")
    project.writes.clear()
    token = run(board, project, recover=True, preview=True)["review_token"]
    apply = dict(recover=True, review_token=token, reviewed_item_ids=[item_id])
    if stage != "apply":
        run(board, project, **apply)
    if stage == "before create":
        inventory = project.get_project_items

        def edit_after_snapshot(project_id):
            snapshot = inventory(project_id)
            project.items[0].update(change)
            return snapshot

        monkeypatch.setattr(project, "get_project_items", edit_after_snapshot)
    else:
        project.items[0].update(change)
    path = tmp_path / ".kanban.json"
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(ConfigurationError, match="current preview|item-0"):
        run(board, project, **(apply if stage == "apply" else {}))
    assert (path.read_bytes() if path.exists() else None) == before
    assert project.writes == []
    current = run(board, project, recover=True, preview=True)
    assert current["review_token"] != token and current["unreviewed_item_ids"] == [item_id]


def test_new_draft_marker_binding_preserves_edit_after_final_draft_read(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Task\n")
    project = Project()
    path = tmp_path / ".kanban.json"
    query = project._query

    def edit_before_marker_write(document, variables):
        if "mutation" in document:
            project.items[0]["body"] = "Collaborator's latest notes"
        return query(document, variables)

    monkeypatch.setattr(project, "_query", edit_before_marker_write)
    run(board, project)
    assert project.items[0]["body"] == "Collaborator's latest notes"
    path.unlink()
    assert run(board, project, recover=True, preview=True)["owned"] == {"Task": "item-0"}


def test_old_sidecar_upgrades_only_on_apply(tmp_path):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Legacy\n")
    project = Project()
    item_id = project.create_draft_issue("project", "Legacy", "Shared notes")
    path = tmp_path / ".kanban.json"
    original = json.dumps({"schema_version": 1, "board_key": KEY, "tasks": {
        "Legacy": {"item_id": item_id, "status": "Todo"}
    }}).encode()
    path.write_bytes(original)
    run(board, project, recover=True, preview=True)
    assert path.read_bytes() == original
    run(board, project, recover=True)
    assert json.loads(path.read_bytes())["schema_version"] == 2
    assert project.items[0]["body"] == "Shared notes"


def test_provenance_write_lost_response_retries_without_second_create(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] New\n")
    project = Project()
    query = project._query

    def lose_marker_response(document, variables):
        result = query(document, variables)
        if "updateProjectV2ItemFieldValue" in document:
            raise GitHubAPIError("lost field write response")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(project, "_query", lose_marker_response)
        with pytest.raises(GitHubAPIError, match="lost field write response"):
            run(board, project)
    run(board, project)
    assert project.writes == [("create", "item-0"), ("mark", "item-0"), ("update", "item-0")]


def test_unusable_provenance_field_blocks_create_before_mutation(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] New\n")
    client = GitHubClient("unused")
    monkeypatch.setattr(client, "get_repo_project", Project().get_repo_project)

    def query(document, variables):
        assert "mutation" not in document
        return {"data": {"node": {"provenanceField": {"dataType": "NUMBER", "id": "other-field"},
                                  "items": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}}

    monkeypatch.setattr(client, "_query", query)
    with pytest.raises(ConfigurationError, match="must be a text field"):
        run(board, client)
    assert not (tmp_path / ".kanban.json").exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_archived_bound_draft_blocks_replacement_by_original_task_key(tmp_path, monkeypatch, corrupt):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] First\n* [ ] Still local\n")
    path = tmp_path / ".kanban.json"
    if corrupt:
        path.write_bytes(b"{")
    client = GitHubClient("unused")
    monkeypatch.setattr(client, "get_repo_project", Project().get_repo_project)

    def query(document, variables):
        assert "archivedStates: [NOT_ARCHIVED, ARCHIVED]" in document
        node = {"id": "archived-id", "isArchived": True, "fieldValueByName": None, "provenance": None,
                "content": {"__typename": "DraftIssue", "id": "draft-id", "title": "Renamed",
                            "body": f"<!-- kanbanger:item:v2 {KEY} 5374696c6c206c6f63616c archived-id -->"}}
        return {"data": {"node": {"provenanceField": None,
                                  "items": {"nodes": [node], "pageInfo": {"hasNextPage": False}}}}}

    monkeypatch.setattr(client, "_query", query)
    for options in ({"recover": True, "preview": True}, {"recover": True}, {}):
        message = "corrupt" if corrupt and not options else "archived-id.*Still local"
        with pytest.raises(ConfigurationError, match=message):
            run(board, client, **options)
    if corrupt:
        assert path.read_bytes() == b"{"
    else:
        assert not path.exists()
    assert not list(tmp_path.glob(".kanban.json.*"))


@pytest.mark.parametrize(("action", "valid_sidecar"), [
    ("ARCHIVE", False), ("UPDATE", False), ("UPDATE", True),
])
def test_conversion_after_ownership_read_leaves_recovered_item_pending(
    tmp_path, monkeypatch, capsys, action, valid_sidecar
):
    board = tmp_path / "_kanban.md"
    path = tmp_path / ".kanban.json"
    board_at(board, "## TODO\n* [ ] Removed\n")
    project = Project()
    run(board, project)
    item, = project.items
    if valid_sidecar:
        item["status"] = "Done"
    else:
        board_at(board, "## TODO\n" if action == "ARCHIVE" else "## DONE\n* [ ] Removed\n")
        path.unlink()
    initial_status = item["status"]
    run(board, project, recover=True)
    project.writes.clear()
    verify = project.verify_owned_draft

    def convert_after_read(*args):
        verify(*args)
        item["type"] = "Issue"

    monkeypatch.setattr(project, "verify_owned_draft", convert_after_read)
    run(board, project)
    assert item["type"] == "Issue" and not item["archived"]
    assert project.writes == []
    record = json.loads(path.read_bytes())["tasks"]["Removed"]
    assert record["item_id"] == item["id"] and record["status"] == item["status"] == initial_status
    output = capsys.readouterr().out
    assert f"PENDING_{action}" in output and item["id"] in output
    with pytest.raises(ConfigurationError, match=f"{item['id']}.*repository issue"):
        run(board, project)
    assert project.writes == [] and "Removed" in json.loads(path.read_bytes())["tasks"]


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


@pytest.mark.parametrize("claimed_title", ["Removed", "Different"])
def test_corrupt_claim_for_archived_draft_requires_matching_marker(tmp_path, claimed_title):
    board = tmp_path / "_kanban.md"
    board_at(board, "## TODO\n* [ ] Removed\n")
    project = Project()
    run(board, project)
    board_at(board, "## TODO\n")
    item, = project.items
    item.update(archived=True, title="Renamed")
    path = tmp_path / ".kanban.json"
    corrupt = json.dumps({"tasks": {claimed_title: {"item_id": item["id"], "status": 42}}}).encode()
    path.write_bytes(corrupt)
    project.writes.clear()
    if claimed_title == "Removed":
        preview = run(board, project, recover=True, preview=True)
        assert preview["archived"] == {item["id"]: "Removed"}
        assert preview["owned"] == {} and preview["plan"]["operations"] == []
        assert path.read_bytes() == corrupt and not list(tmp_path.glob(".kanban.json.corrupt-*"))
        report = run(board, project, recover=True)
        assert Path(report["backup"]).read_bytes() == corrupt
        run(board, project)
        assert json.loads(path.read_bytes())["tasks"] == {}
    else:
        for preview in (True, False):
            with pytest.raises(ConfigurationError, match="item-0.*Different"):
                run(board, project, recover=True, preview=preview)
        assert path.read_bytes() == corrupt and not list(tmp_path.glob(".kanban.json.corrupt-*"))
    assert item["archived"] and project.writes == []


def test_cr_only_legacy_board_keeps_present_task_and_line_endings(tmp_path):
    board = tmp_path / "_kanban.md"
    source = b"# Board\r## TODO\r* [ ] Present\r"
    board.write_bytes(source)
    project = Project()
    item_id = project.create_draft_issue("project", "Present")
    (tmp_path / ".kanban.json").write_text(json.dumps({"tasks": {
        "Present": {"item_id": item_id, "status": "Todo"},
    }}))
    preview = run(board, project, recover=True, preview=True)
    assert preview["plan"]["summary"]["archive"] == 0
    assert "kanbanger init" in preview["board_key_guidance"]
    with pytest.raises(ConfigurationError, match="board-id"):
        run(board, project)
    assert board.read_bytes() == source
    # An operator installs the marker with the editor that owns the board.
    keyed = source.replace(b"# Board\r", f"# Board\r<!-- kanbanger:board-id: {KEY} -->\r".encode())
    board.write_bytes(keyed)
    preview = run(board, project, recover=True, preview=True)
    assert preview["pending_legacy_claims"] == {item_id: "Present"}
    run(board, project, recover=True, review_token=preview["review_token"], confirmed_legacy_item_ids=[item_id])
    assert project.items[0]["body"] == ""
    with pytest.raises(ConfigurationError, match="binding pending"):
        run(board, project)
    assert not project.items[0]["archived"]
    assert board.read_bytes() == keyed
    assert run(board, project, recover=True, preview=True)["owned"] == {"Present": item_id}


def test_sync_detects_keyed_board_edit_during_inventory(tmp_path, monkeypatch):
    board = tmp_path / "_kanban.md"
    source = f"# Board\r\n<!-- kanbanger:board-id: {KEY} -->\r\n## TODO\r\n".encode()
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
    path.write_text(json.dumps({"board_key": KEY, "tasks": {"Legacy": {"item_id": item_id, "status": "Todo"}}}))
    before = path.read_bytes()
    project.writes.clear()
    inventory = project.get_project_items

    def edit_draft(project_id):
        snapshot = inventory(project_id)
        project.items[0]["body"] = "Notes\n\nNew paragraph"
        return snapshot

    monkeypatch.setattr(project, "get_project_items", edit_draft)
    with pytest.raises(ConfigurationError, match="binding pending for item-0"):
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


@pytest.mark.parametrize("recover", [False, True])
def test_editor_save_after_board_verification_survives(tmp_path, monkeypatch, recover):
    board = tmp_path / "_kanban.md"
    original = b"# Board\r\n## TODO\r\n* [ ] First task\r\n"
    edited = original + b"* [ ] Saved by editor\r\n"
    board.write_bytes(original)
    project = Project()
    verify = LocalBoard.verify_source

    def verify_then_save(local_board):
        verify(local_board)
        board.write_bytes(edited)

    monkeypatch.setattr(LocalBoard, "verify_source", verify_then_save)
    error = None
    try:
        run(board, project, recover=recover)
    except ConfigurationError as exc:
        error = str(exc)
    assert board.read_bytes() == edited
    assert error and "board-id" in error and "kanbanger init" in error
    assert project.writes == []
    assert not (tmp_path / ".kanban.json").exists()
    assert not list(tmp_path.glob("_kanban.md.tmp.*"))


@pytest.mark.parametrize("action", ["UPDATE", "ARCHIVE", "CREATE"])
@pytest.mark.parametrize("change", [
    "converted", "archived", "removed marker", "other board", "other title",
    "other item", "unbound marker", "duplicated marker",
])
def test_changed_mutation_target_is_left_alone(tmp_path, monkeypatch, action, change):
    board = tmp_path / "_kanban.md"
    path = tmp_path / ".kanban.json"
    project = Project()
    board_at(board, "## TODO\n* [ ] One\n")

    def change_target():
        item, = project.items
        if change == "converted":
            item["type"] = "Issue"
        elif change == "archived":
            item["archived"] = True
        else:
            if change in ("removed marker", "unbound marker"):
                del item["provenance"]
            item["body"] = {
                "removed marker": "Edited notes",
                "other board": f"<!-- kanbanger:item:v2 {'b' * 32} 4f6e65 item-0 -->",
                "other title": f"<!-- kanbanger:item:v2 {KEY} 54776f item-0 -->",
                "other item": f"<!-- kanbanger:item:v2 {KEY} 4f6e65 item-1 -->",
                "unbound marker": f"<!-- kanbanger:item:v1 {KEY} 4f6e65 -->",
                "duplicated marker": item["body"] + "\n" + item["body"],
            }[change]

    if action == "CREATE":
        mark = project.mark_draft

        def change_after_mark(*args):
            mark(*args)
            change_target()

        monkeypatch.setattr(project, "mark_draft", change_after_mark)
    else:
        run(board, project)
        before = path.read_bytes()
        project.writes.clear()
        board_at(board, "## DONE\n* [ ] One\n" if action == "UPDATE" else "## TODO\n")
        inventory = project.get_project_items

        def change_after_inventory(project_id):
            snapshot = inventory(project_id)
            change_target()
            return snapshot

        monkeypatch.setattr(project, "get_project_items", change_after_inventory)

    with pytest.raises(ConfigurationError, match="item-0"):
        run(board, project)
    if action == "CREATE":
        assert project.writes == [("create", "item-0"), ("mark", "item-0")]
        assert json.loads(path.read_bytes())["tasks"]["One"]["status"] is None
    else:
        assert project.writes == []
        assert path.read_bytes() == before
