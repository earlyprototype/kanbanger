"""Tests for Bug A: add_task should append (not prepend) and pad properly.

Background: v2.1.0 (and Bundle 1 of partymix) inserted new tasks at the
line directly after the column header with no blank-line padding, producing
output like::

    ## TODO
    *   [ ] new task    <- no blank between header and task
    *   [ ] old task 1
    *   [ ] old task 2

The fix rebuilds the column canonically: header -> blank -> tasks (in
original order, new one appended at bottom) -> blank.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest


def _read_board(workspace: Path) -> str:
    return (workspace / "_kanban.md").read_text(encoding="utf-8")


def test_add_to_empty_column_pads_blank_line_after_header(
    kanban_workspace: Path,
    registered_tools: dict[str, Callable],
):
    """Empty column gets header -> blank -> task -> blank shape on first add."""
    add_task = registered_tools["add_task"]

    result = add_task(title="First task", column="TODO")
    assert "Successfully" in result, result

    board = _read_board(kanban_workspace)
    assert "## TODO\n\n*   [ ] First task\n\n## DOING" in board, (
        f"Expected canonical shape between TODO and DOING, got:\n{board}"
    )


def test_add_to_populated_column_appends_at_bottom(
    kanban_workspace: Path,
    registered_tools: dict[str, Callable],
):
    """A second add_task lands AFTER the first, not before it."""
    add_task = registered_tools["add_task"]

    add_task(title="First task", column="TODO")
    add_task(title="Second task", column="TODO")

    board = _read_board(kanban_workspace)
    assert (
        "## TODO\n\n*   [ ] First task\n*   [ ] Second task\n\n## DOING" in board
    ), f"Expected First then Second under TODO, got:\n{board}"


def test_add_then_delete_restores_seed_shape(
    kanban_workspace: Path,
    registered_tools: dict[str, Callable],
):
    """Round-trip: add then delete should leave _kanban.md byte-for-byte unchanged."""
    add_task = registered_tools["add_task"]
    delete_task = registered_tools["delete_task"]

    seed = _read_board(kanban_workspace)
    add_task(title="Round-trip task", column="DOING")
    delete_task(title="Round-trip task", column="DOING")
    after = _read_board(kanban_workspace)

    assert after == seed, (
        "Round-trip mutated the board.\n"
        f"--- BEFORE ---\n{seed}\n--- AFTER ---\n{after}"
    )


def test_description_is_preserved_in_appended_task(
    kanban_workspace: Path,
    registered_tools: dict[str, Callable],
):
    """Bonus: ensure description suffix still appears with the appended task."""
    add_task = registered_tools["add_task"]
    add_task(title="With desc", column="TODO", description="Important context")

    board = _read_board(kanban_workspace)
    assert "*   [ ] With desc - Important context" in board, (
        f"Expected description suffix, got:\n{board}"
    )
