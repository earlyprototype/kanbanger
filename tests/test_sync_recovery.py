"""Restart sync against the same offline Project after an interrupted CREATE."""

import json
import os
import socket
import sys
from contextlib import ExitStack

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError
from urllib3.exceptions import ProxyError as Urllib3ProxyError

import kanban_io
from sync_kanban import ConfigurationError, GitHubAPIError, GitHubClient, LocalBoard
from sync_kanban import StateManager, Syncer


class DraftProject(GitHubClient):
    def __init__(self):
        self.cards = {}
        self.lose_response = False
        self.project_id = "project"

    def get_repo_project(self, owner, repo, project_number):
        return "repo", self.project_id, "status", {"Todo": "todo-option"}

    def _query(self, query, variables, **kwargs):
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


def retry_and_check_binding(board, project):
    sync(board, project)
    assert list(project.cards) == ["card-1"]
    saved = StateManager(str(board)).load()
    assert saved["tasks"] == {"New": {"item_id": "card-1", "status": "Todo"}}
    assert "pending_create" not in saved


def test_retry_after_https_proxy_dns_failure(board, monkeypatch):
    project = DraftProject()
    client = GitHubClient("offline")
    monkeypatch.setattr(client, "get_repo_project", project.get_repo_project)

    def offline_dns(host, *args, **kwargs):
        assert host == "proxy.invalid"
        raise socket.gaierror(socket.EAI_NONAME, "proxy DNS lookup failed")

    monkeypatch.setattr(socket, "getaddrinfo", offline_dns)
    with requests.Session() as session:
        session.trust_env = False
        session.proxies["https"] = "https://proxy.invalid:443"
        monkeypatch.setattr(requests, "post", session.post)
        with pytest.raises(GitHubAPIError) as failure:
            sync(board, client)

    assert isinstance(failure.value.__cause__, requests.exceptions.ProxyError)
    assert not board.with_name(".kanban.json").exists()
    retry_and_check_binding(board, project)


@pytest.mark.parametrize(("failure", "accepted"), [
    (requests.ConnectTimeout("timed out"), False),
    (requests.ConnectionError(MaxRetryError(None, "/", NewConnectionError(None, "refused"))), False),
    (requests.exceptions.ProxyError(
        MaxRetryError(None, "/", Urllib3ProxyError("proxy reset", ProtocolError()))
    ), True),
    (requests.ReadTimeout("timed out"), True),
    (requests.ConnectionError("connection reset"), True),
], ids=["connect-timeout", "connection-refused", "proxy-reset",
        "read-timeout", "connection-reset"])
def test_retry_after_create_transport_failure(board, monkeypatch, failure, accepted):
    project = DraftProject()
    client = GitHubClient("offline")
    monkeypatch.setattr(client, "get_repo_project", project.get_repo_project)

    def fail_post(url, *, headers, json, **kwargs):
        assert "addProjectV2DraftIssue" in json["query"]
        if accepted:
            project._query(json["query"], json["variables"])
        raise failure

    monkeypatch.setattr(requests, "post", fail_post)
    with pytest.raises(GitHubAPIError):
        sync(board, client)

    saved = StateManager(str(board)).load()
    assert saved["tasks"] == {}
    if accepted:
        assert list(project.cards) == ["card-1"]
        assert saved["pending_create"]["title"] == "New"
    else:
        assert project.cards == {}
        assert "pending_create" not in saved
        assert not board.with_name(".kanban.json").exists()

    retry_and_check_binding(board, project)


@pytest.mark.parametrize("status", [401, 403, 502])
def test_retry_after_create_http_error(board, monkeypatch, status):
    project = DraftProject()
    client = GitHubClient("offline")
    monkeypatch.setattr(client, "get_repo_project", project.get_repo_project)

    def failed_response(url, *, json, **kwargs):
        assert "addProjectV2DraftIssue" in json["query"]
        if status == 502:
            project._query(json["query"], json["variables"])
        response = requests.Response()
        response.status_code = status
        response._content = b""
        return response

    monkeypatch.setattr(requests, "post", failed_response)
    with pytest.raises(GitHubAPIError, match=f"status {status}"):
        sync(board, client)

    if status == 502:
        assert StateManager(str(board)).load()["pending_create"]["title"] == "New"
    else:
        assert not board.with_name(".kanban.json").exists()
    retry_and_check_binding(board, project)


def test_unsent_create_preserves_previous_bindings_and_sync_time(board, monkeypatch):
    state_file = board.with_name(".kanban.json")
    bindings = {"Existing": {"item_id": "older-card", "status": "Todo"}}
    previous = {
        "repo_node_id": "previous-repo", "project_id": "previous-project",
        "tasks": bindings, "board_key": "original-board", "extra": ["preserve"],
    }
    checkpoint = (json.dumps(previous) + "\n").encode()
    state_file.write_bytes(checkpoint)
    os.utime(state_file, (1, 1))
    previous_mtime = state_file.stat().st_mtime_ns
    client = GitHubClient("offline")
    monkeypatch.setattr(client, "get_repo_project", DraftProject().get_repo_project)

    def refuse_create(url, *, headers, json, **kwargs):
        assert "addProjectV2DraftIssue" in json["query"]
        raise requests.ConnectTimeout("timed out")

    monkeypatch.setattr(requests, "post", refuse_create)
    with pytest.raises(GitHubAPIError):
        sync(board, client)

    saved = StateManager(str(board)).load()
    assert saved["tasks"] == bindings
    assert "pending_create" not in saved
    assert state_file.read_bytes() == checkpoint
    assert state_file.stat().st_mtime_ns == previous_mtime


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing")
def test_first_create_rollback_waits_for_sidecar_reader(board, monkeypatch):
    project = DraftProject()
    client = GitHubClient("offline")
    monkeypatch.setattr(client, "get_repo_project", project.get_repo_project)
    state_file = board.with_name(".kanban.json")
    with ExitStack() as readers:
        def fail_before_send(url, *, json, **kwargs):
            assert "addProjectV2DraftIssue" in json["query"]
            readers.enter_context(state_file.open("rb"))
            raise requests.ConnectTimeout("timed out")

        monkeypatch.setattr(requests, "post", fail_before_send)
        # Release the real read handle during the retry delay.
        monkeypatch.setattr(kanban_io.time, "sleep", lambda delay: readers.close())
        with pytest.raises(GitHubAPIError, match="timed out"):
            sync(board, client)

    assert not state_file.exists()
    retry_and_check_binding(board, project)


def test_create_redirect_keeps_intent_if_the_first_request_reached_the_project(board, monkeypatch):
    project = DraftProject()
    client = GitHubClient("offline")
    monkeypatch.setattr(client, "get_repo_project", project.get_repo_project)

    def redirect(adapter, request, **kwargs):
        if request.url.endswith("/redirected"):
            raise requests.ConnectTimeout("redirect destination unavailable")
        body = json.loads(request.body)
        project._query(body["query"], body["variables"])
        response = requests.Response()
        response.status_code = 307
        response.headers["Location"] = "/redirected"
        response.request = request
        response.url = request.url
        response._content = b""
        return response

    with requests.Session() as session:
        session.trust_env = False
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", redirect)
        monkeypatch.setattr(requests, "post", session.post)
        with pytest.raises(GitHubAPIError):
            sync(board, client)

    assert StateManager(str(board)).load()["pending_create"]["title"] == "New"
    sync(board, project)
    assert list(project.cards) == ["card-1"]
    assert StateManager(str(board)).load()["tasks"]["New"]["item_id"] == "card-1"


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
    syncer = Syncer(LocalBoard(str(board)), StateManager(str(board)), project)

    def disk_full(state):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(StateManager, "save_locked", disk_full)
        with pytest.raises(OSError, match="disk full"):
            syncer.sync("owner/repo")
    assert project.cards == {}
    assert not board.with_name(".kanban.json").exists()

    syncer.sync("owner/repo")
    assert list(project.cards) == ["card-1"]
    saved = StateManager(str(board)).load()
    assert saved["tasks"] == {"New": {"item_id": "card-1", "status": "Todo"}}
    assert "pending_create" not in saved


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
