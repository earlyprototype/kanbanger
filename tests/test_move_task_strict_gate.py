"""Tests for move_task strict-gate enforcement (Bundle 1b item 2)."""

from __future__ import annotations

import json


def _seed_task(workspace, title, column):
    """Append a task to the given column in the fixture board."""
    board = workspace / "_kanban.md"
    text = board.read_text(encoding="utf-8")
    text = text.replace(
        f"## {column}\n",
        f"## {column}\n*   [ ] {title}\n",
        1,
    )
    board.write_text(text, encoding="utf-8")


# --- happy cases: legitimate REVIEW-gate transitions ---


def test_move_task_review_to_done_passes_gate(registered_tools, kanban_workspace):
    """REVIEW -> DONE is the only legal direct-to-DONE move."""
    _seed_task(kanban_workspace, "Task A", "REVIEW")
    move_task = registered_tools["move_task"]

    result = move_task("Task A", "REVIEW", "DONE")

    assert "Successfully moved" in result

    board = (kanban_workspace / "_kanban.md").read_text(encoding="utf-8")
    done = board.split("## DONE")[1]
    assert "*   [x] Task A" in done
    review = board.split("## REVIEW")[1].split("##")[0]
    assert "Task A" not in review


def test_move_task_non_done_transitions_unchanged(registered_tools, kanban_workspace):
    """Non-DONE transitions are not affected by the gate."""
    _seed_task(kanban_workspace, "Task A", "TODO")
    move_task = registered_tools["move_task"]

    result = move_task("Task A", "TODO", "DOING")

    assert "Successfully moved" in result
    board = (kanban_workspace / "_kanban.md").read_text(encoding="utf-8")
    doing = board.split("## DOING")[1].split("##")[0]
    assert "Task A" in doing


# --- reject cases: each non-REVIEW -> DONE transition fails ---


def test_move_task_doing_to_done_gate_violation(registered_tools, kanban_workspace):
    _seed_task(kanban_workspace, "Task A", "DOING")
    move_task = registered_tools["move_task"]

    result = json.loads(move_task("Task A", "DOING", "DONE"))

    assert result["success"] is False
    assert result["error_code"] == "gate_violation"
    assert result["context"]["from_column"] == "DOING"
    assert result["context"]["to_column"] == "DONE"
    assert result["context"]["actual_column"] == "DOING"
    assert "propose_done" in result["context"]["canonical_path"]
    assert "approve_done" in result["context"]["canonical_path"]
    assert "propose_done" in result["message"]
    assert "approve_done" in result["message"]


def test_move_task_todo_to_done_gate_violation(registered_tools, kanban_workspace):
    _seed_task(kanban_workspace, "Task A", "TODO")
    move_task = registered_tools["move_task"]

    result = json.loads(move_task("Task A", "TODO", "DONE"))

    assert result["success"] is False
    assert result["error_code"] == "gate_violation"
    assert result["context"]["from_column"] == "TODO"
    assert result["context"]["actual_column"] == "TODO"


def test_move_task_backlog_to_done_gate_violation(registered_tools, kanban_workspace):
    _seed_task(kanban_workspace, "Task A", "BACKLOG")
    move_task = registered_tools["move_task"]

    result = json.loads(move_task("Task A", "BACKLOG", "DONE"))

    assert result["success"] is False
    assert result["error_code"] == "gate_violation"
    assert result["context"]["from_column"] == "BACKLOG"


def test_move_task_gate_violation_for_nonexistent_task(registered_tools, kanban_workspace):
    """Gate fires on (from, to) pair regardless of board state. actual_column=None on miss."""
    move_task = registered_tools["move_task"]

    result = json.loads(move_task("Nonexistent", "DOING", "DONE"))

    assert result["success"] is False
    assert result["error_code"] == "gate_violation"
    assert result["context"]["actual_column"] is None


# --- state-non-mutation guarantee ---


def test_gate_violation_does_not_mutate_board(registered_tools, kanban_workspace):
    """Gate violation is caller-error; the board must be untouched."""
    _seed_task(kanban_workspace, "Task A", "DOING")
    board = kanban_workspace / "_kanban.md"
    before = board.read_text(encoding="utf-8")

    move_task = registered_tools["move_task"]
    move_task("Task A", "DOING", "DONE")

    after = board.read_text(encoding="utf-8")
    assert before == after, "gate_violation must not mutate the board"
