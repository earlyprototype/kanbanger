"""Restart sync against the same offline Project after an interrupted CREATE."""

import json

import pytest

from sync_kanban import ConfigurationError, GitHubAPIError, GitHubClient, LocalBoard
from sync_kanban import StateManager, Syncer


class DraftProject(GitHubClient):
    def __init__(self):
        self.cards = {}
        self.lose_response = False
        self.project_id = "project"

    def get_repo_project(self, owner, repo, project_number):
        return "repo", self.project_id, "status", {"Todo": "todo-option"}

    def _query(self, query, variables):
        assert variables["projectId"] == self.project_id
        if "addProjectV2DraftIssue" in query:
            item_id = f"card-{len(self.cards) + 1}"
            self.cards[item_id] = {
                "title": variables["title"], "body": variables["body"],
            }
            if self.lose_response:
                self.lose_response = False
                raise GitHubAPIError("response lost")
            return {"data": {"addProjectV2DraftIssue": {"projectItem": {"id": item_id}}}}
        if "items(first:" in query:
            # A same-title draft from a different board appears on page one.
            first_page = variables["cursor"] is None
            cards = {"other": {"title": "New", "body": ""}} if first_page else self.cards
            return {"data": {"node": {"items": {
                "nodes": [{"id": key, "content": card} for key, card in cards.items()],
                "pageInfo": {"hasNextPage": first_page, "endCursor": "next"},
            }}}}
        if "updateProjectV2ItemFieldValue" in query:
            self.cards[variables["itemId"]]["status"] = variables["optionId"]
            return {}
        pytest.fail(f"Unexpected GraphQL operation: {query}")


@pytest.fixture
def board(kanban_workspace):
    path = kanban_workspace / "_kanban.md"
    path.write_text("## TODO\n* [ ] New\n", encoding="utf-8")
    return path


def sync(board, project):
    Syncer(LocalBoard(str(board)), StateManager(str(board)), project).sync("owner/repo")


@pytest.mark.parametrize("failure", ["response", "binding"])
def test_restart_binds_the_accepted_card(board, monkeypatch, failure):
    project = DraftProject()
    if failure == "response":
        project.lose_response = True
        with pytest.raises(GitHubAPIError, match="response lost"):
            sync(board, project)
    else:
        save = StateManager.save_locked

        def fail_binding(state):
            if state.get_item_id("New"):
                raise OSError("binding write failed")
            save(state)

        with monkeypatch.context() as patch:
            patch.setattr(StateManager, "save_locked", fail_binding)
            with pytest.raises(OSError, match="binding write failed"):
                sync(board, project)

    assert list(project.cards) == ["card-1"]
    sync(board, project)
    assert list(project.cards) == ["card-1"]
    assert project.cards["card-1"]["status"] == "todo-option"
    saved = StateManager(str(board)).load()
    assert saved["tasks"] == {"New": {"item_id": "card-1", "status": "Todo"}}
    assert "pending_create" not in saved


@pytest.mark.parametrize("outcome", [
    "missing marker", "two matches", "other project", "already bound",
])
def test_uncertain_create_stops_before_further_mutations(board, outcome):
    project = DraftProject()
    project.lose_response = True
    with pytest.raises(GitHubAPIError):
        sync(board, project)
    if outcome == "missing marker":
        project.cards["card-1"]["body"] = ""
    elif outcome == "two matches":
        project.cards["copy"] = project.cards["card-1"].copy()
    elif outcome == "other project":
        project.project_id = "different-project"
    else:
        state = StateManager(str(board))
        state.load()
        state.update_task("Existing", "card-1", "Todo")
        state.save()

    # Even a replacement task must wait for reconciliation of the removed one.
    board.write_text("## TODO\n* [ ] Replacement\n", encoding="utf-8")
    before = json.dumps(project.cards)
    state_path = board.with_name(".kanban.json")
    pending = state_path.read_bytes()
    with pytest.raises(ConfigurationError, match="pending create"):
        sync(board, project)
    assert state_path.read_bytes() == pending
    assert json.dumps(project.cards) == before


def test_failed_intent_write_prevents_create(board, monkeypatch):
    project = DraftProject()

    def disk_full(state):
        raise OSError("disk full")

    monkeypatch.setattr(StateManager, "save_locked", disk_full)
    with pytest.raises(OSError, match="disk full"):
        sync(board, project)
    assert project.cards == {}


def test_preview_reports_pending_create_without_changing_state(board, registered_tools):
    project = DraftProject()
    project.lose_response = True
    with pytest.raises(GitHubAPIError):
        sync(board, project)
    state_path = board.with_name(".kanban.json")
    pending = state_path.read_bytes()
    preview = json.loads(registered_tools["sync_to_github"](dry_run=True))
    assert preview["error_code"] == "configuration_error"
    assert "pending create" in preview["message"]
    assert state_path.read_bytes() == pending


def test_damaged_pending_record_requires_reconciliation(board):
    state = board.with_name(".kanban.json")
    damaged = '{"tasks": {}, "pending_create": {"title": "New"}}'
    state.write_text(damaged, encoding="utf-8")
    project = DraftProject()
    with pytest.raises(ConfigurationError, match="pending create"):
        sync(board, project)
    assert project.cards == {}
    assert state.read_text(encoding="utf-8") == damaged
