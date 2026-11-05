"""Creation must not bypass the REVIEW approval boundary for canonical DONE."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kanbanger.server import create_server


BOARD = (
    b"# Review-gated board\r\n\r\n"
    b"## BACKLOG\r\n\r\n"
    b"## TODO\r\n\r\n"
    b"## DOING\r\n\r\n"
    b"## REVIEW\r\n\r\n"
    b"## DONE\r\n\r\n*   [x] Previously approved work\r\n\r\n"
    b"## VERIFY\r\n\r\n"
)


def _call(server, name: str, arguments: dict) -> str:
    result = asyncio.run(server.call_tool(name, arguments))
    content = result[0] if isinstance(result, tuple) else result
    return "\n".join(block.text for block in content if hasattr(block, "text"))


def test_add_task_cannot_create_directly_in_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    board = tmp_path / "_kanban.md"
    board.write_bytes(BOARD)
    server = create_server()

    output = _call(server, "add_task", {
        "title": "Unreviewed work", "column": "DONE", "description": "Do not publish",
    })

    # Check the actual mutation first: the old implementation silently adds it.
    assert board.read_bytes() == BOARD
    assert set(tmp_path.iterdir()) == {board}
    payload = json.loads(output)
    assert payload["success"] is False
    assert payload["error_code"] == "gate_violation"
    assert "REVIEW" in payload["message"]
    assert "approve_done" in payload["message"]


@pytest.mark.parametrize("column", ["BACKLOG", "TODO", "DOING", "REVIEW", "VERIFY"])
def test_add_task_keeps_non_done_creation_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: str,
):
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    board = tmp_path / "_kanban.md"
    board.write_bytes(BOARD)
    server = create_server()

    output = _call(server, "add_task", {
        "title": "Compatible\ttask", "column": column, "description": "Keep this detail",
    })

    assert output == f"Successfully added task 'Compatible task' to {column}"
    text = board.read_text(encoding="utf-8")
    section = text.split(f"## {column}\n", 1)[1].split("\n## ", 1)[0]
    assert "*   [ ] Compatible task - Keep this detail" in section
    assert "*   [x] Previously approved work" in text


def test_created_work_can_reach_done_via_explicit_review_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    (tmp_path / "_kanban.md").write_bytes(BOARD)
    server = create_server()

    _call(server, "add_task", {"title": "Reviewed work", "column": "DOING"})
    proposed = json.loads(_call(server, "propose_done", {"title": "Reviewed work"}))
    assert proposed["success"] is True
    tasks = json.loads(_call(server, "list_tasks", {}))
    assert "Reviewed work" in tasks["REVIEW"]
    assert "Reviewed work" not in tasks["DONE"]

    approved = json.loads(_call(server, "approve_done", {"title": "Reviewed work"}))
    assert approved["success"] is True
    tasks = json.loads(_call(server, "list_tasks", {}))
    assert "Reviewed work" in tasks["DONE"]
    assert "Reviewed work" not in tasks["REVIEW"]
