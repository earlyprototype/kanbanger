"""Operator-run recovery test for a disposable, linked GitHub Project."""

import json
import os
from uuid import uuid4

import pytest

from kanban_io import format_board_key_marker
from sync_kanban import ConfigurationError, GitHubClient, LocalBoard, StateManager, Syncer


@pytest.mark.skipif(not os.getenv("KANBANGER_SANDBOX_REPO"), reason="requires a live sandbox")
def test_recover_in_github_project(tmp_path):
    repo = os.environ["KANBANGER_SANDBOX_REPO"]
    number = int(os.environ["KANBANGER_SANDBOX_PROJECT_NUMBER"])
    client = GitHubClient(os.environ["GITHUB_TOKEN"])
    _, project, _, _ = client.get_repo_project(*repo.split("/"), number)
    key = uuid4().hex
    title = f"Recovery test {key}"
    board = tmp_path / "_kanban.md"
    header = f"# Sandbox\n{format_board_key_marker(key)}\n## TODO\n"
    board.write_text(header + f"* [ ] {title}\n", encoding="utf-8")

    def sync(**options):
        return Syncer(LocalBoard(str(board)), StateManager(str(board)), client).sync(
            repo, number, **options
        )

    # Record this run's IDs, including a draft created before a later failure.
    created = set()
    recovered = {}
    create = client.create_draft_issue

    def record_create(project_id, title, body=""):
        item_id = create(project_id, title, body)
        created.add(item_id)
        return item_id

    client.create_draft_issue = record_create
    try:
        # Every item in this snapshot predates the test board's first CREATE.
        initial = sync(recover=True, preview=True)
        sync(recover=True, reviewed_item_ids=initial["unreviewed_item_ids"], review_token=initial["review_token"])
        sync()
        owned, = created
        sentinel = record_create(project, title, "Independent sandbox draft")
        before = next(row for row in client.get_project_items(project) if row["id"] == sentinel)
        board.write_text(header.replace("## TODO", "## DONE") + f"* [ ] {title}\n", encoding="utf-8")
        path = tmp_path / ".kanban.json"
        for corrupt in (False, True):
            path.unlink()
            if corrupt:
                path.write_bytes(b"{")
            with pytest.raises(ConfigurationError, match="recover"):
                sync()
            preview = sync(recover=True, preview=True)
            assert preview["owned"] == {title: owned}
            recovered["corrupt" if corrupt else "missing"] = preview["owned"][title]
            assert preview["unmarked"][sentinel] == title
            assert [(op["action"], op["item_id"]) for op in preview["plan"]["operations"]] == [
                ("PENDING_UPDATE", owned)
            ]
            assert preview == sync(recover=True, preview=True)
            sync(recover=True)
            sync()
            assert created == {owned, sentinel}
            current = next(row for row in client.get_project_items(project) if row["id"] == owned)
            assert current["status"] == preview["plan"]["operations"][0]["from_status"]
        board.write_text(header, encoding="utf-8")
        path.unlink()
        plan = sync(recover=True, preview=True)["plan"]
        assert [(op["action"], op["item_id"]) for op in plan["operations"]] == [
            ("PENDING_ARCHIVE", owned)
        ]
        sync(recover=True)
        sync()
        items = {row["id"]: row for row in client.get_project_items(project)}
        assert items[owned]["archived"] is False
        assert json.loads(path.read_bytes())["tasks"][title]["item_id"] == owned
        assert items[sentinel] == before
    finally:
        for row in client.get_project_items(project):
            if row["id"] in created and not row["archived"]:
                client.archive_item(project, row["id"])
        archived = sorted(row["id"] for row in client.get_project_items(project)
                          if row["id"] in created and row["archived"])
        assert archived == sorted(created)
    print(json.dumps({"project_id": project, "recovered_item_ids": recovered,
                      "unchanged_sentinel_id": sentinel, "archived_at_cleanup": archived}))
