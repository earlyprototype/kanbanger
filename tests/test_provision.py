"""Tests for kanbanger.provision and the `setup_project` MCP tool.

Covers the issue #15 step 3 provisioning contract:
  - board scaffolded with the canonical 5-column schema when absent,
  - board NEVER clobbered when already present,
  - the agent touchpoint (CLAUDE.md) added idempotently (re-run is a no-op),
  - GitHub-sync slots written as EMPTY placeholders only (no secrets),
  - the `setup_project` MCP tool drives the same shared code,
  - the `kanbanger init` CLI parity entry point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kanbanger import provision
from kanbanger.provision import (
    CLAUDE_MD_END,
    CLAUDE_MD_START,
    build_kanban_board,
    provision_project,
)

CANONICAL_COLUMNS = ["BACKLOG", "TODO", "DOING", "REVIEW", "DONE"]


# --- helpers ---------------------------------------------------------------


def _columns_in(board_text: str) -> list[str]:
    return [
        line.strip()[3:].strip()
        for line in board_text.splitlines()
        if line.strip().startswith("## ")
    ]


# --- board scaffold --------------------------------------------------------


def test_build_kanban_board_has_canonical_five_columns():
    board = build_kanban_board("Demo")
    assert _columns_in(board) == CANONICAL_COLUMNS
    assert board.startswith("# Demo Kanban")


def test_board_scaffolded_when_absent(tmp_path: Path):
    result = provision_project(tmp_path)

    board = tmp_path / "_kanban.md"
    assert board.exists()
    assert _columns_in(board.read_text(encoding="utf-8")) == CANONICAL_COLUMNS
    # The board uses the directory name as the project title.
    assert board.read_text(encoding="utf-8").startswith(f"# {tmp_path.name} Kanban")
    assert any("_kanban.md" in note for note in result.created)


def test_board_not_clobbered_when_present(tmp_path: Path):
    board = tmp_path / "_kanban.md"
    sentinel = "# Pre-existing Board\n\n## BACKLOG\n*   [ ] do not delete me\n"
    board.write_text(sentinel, encoding="utf-8")

    result = provision_project(tmp_path)

    # Byte-for-byte unchanged.
    assert board.read_text(encoding="utf-8") == sentinel
    assert any("_kanban.md" in note for note in result.already_present)
    assert all("_kanban.md" not in note for note in result.created)


# --- CLAUDE.md touchpoint idempotency --------------------------------------


def test_touchpoint_created_when_absent(tmp_path: Path):
    provision_project(tmp_path)
    claude_md = tmp_path / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8")
    assert CLAUDE_MD_START in text
    assert CLAUDE_MD_END in text
    assert "Never hand-edit `_kanban.md`" in text
    assert "project-scoped" in text
    assert "REVIEW gates DONE" in text


def test_touchpoint_idempotent_second_run_is_noop(tmp_path: Path):
    provision_project(tmp_path)
    claude_after_first = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    board_after_first = (tmp_path / "_kanban.md").read_text(encoding="utf-8")
    mcp_after_first = (tmp_path / ".mcp.json").read_text(encoding="utf-8")

    result2 = provision_project(tmp_path)

    # Files are byte-identical after a second run.
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == claude_after_first
    assert (tmp_path / "_kanban.md").read_text(encoding="utf-8") == board_after_first
    assert (tmp_path / ".mcp.json").read_text(encoding="utf-8") == mcp_after_first

    # Exactly one stanza — no duplication.
    assert claude_after_first.count(CLAUDE_MD_START) == 1
    assert claude_after_first.count(CLAUDE_MD_END) == 1

    # Second run created nothing new.
    assert result2.created == []


def test_touchpoint_appended_without_clobbering_existing(tmp_path: Path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# My Project\n\nProject-specific guidance.\n", encoding="utf-8")

    provision_project(tmp_path)

    text = claude_md.read_text(encoding="utf-8")
    assert "Project-specific guidance." in text
    assert CLAUDE_MD_START in text


# --- AGENTS.md: augment only if present ------------------------------------


def test_agents_md_not_created_when_absent(tmp_path: Path):
    provision_project(tmp_path)
    assert not (tmp_path / "AGENTS.md").exists()


def test_agents_md_augmented_when_present(tmp_path: Path):
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# AGENTS\n\nExisting agent guidance.\n", encoding="utf-8")

    provision_project(tmp_path)

    text = agents_md.read_text(encoding="utf-8")
    assert "Existing agent guidance." in text
    assert CLAUDE_MD_START in text


# --- GitHub sync config: empty placeholders, no secrets --------------------


def test_mcp_json_sync_slots_are_empty_placeholders(tmp_path: Path):
    provision_project(tmp_path)
    text = (tmp_path / ".mcp.json").read_text(encoding="utf-8")
    assert "${GITHUB_TOKEN:-}" in text
    assert "${GITHUB_REPO:-}" in text
    assert "${GITHUB_PROJECT_NUMBER:-}" in text
    # Targets the GLOBAL command, not a per-project venv interpreter.
    assert "kanbanger-mcp" in text
    assert ".venv" not in text


def test_mcp_json_not_clobbered_when_present(tmp_path: Path):
    mcp_json = tmp_path / ".mcp.json"
    sentinel = '{"mcpServers": {"kanbanger": {"command": "custom"}}}\n'
    mcp_json.write_text(sentinel, encoding="utf-8")

    result = provision_project(tmp_path)

    assert mcp_json.read_text(encoding="utf-8") == sentinel
    assert any(".mcp.json" in note for note in result.already_present)


# --- error handling --------------------------------------------------------


def test_provision_raises_on_missing_dir(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError):
        provision_project(missing)


# --- summary ---------------------------------------------------------------


def test_summary_mentions_secret_safety(tmp_path: Path):
    result = provision_project(tmp_path)
    summary = result.summary()
    assert "no secrets" in summary.lower()
    assert "GITHUB_TOKEN" in summary


# --- the setup_project MCP tool --------------------------------------------


def _setup_project_tool(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    """Register tools against a stub with KANBANGER_WORKSPACE=workspace and
    return the `setup_project` callable. Importing here (not at module top)
    keeps the env var set before tools read it."""
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(workspace))
    from tests.conftest import _StubMCPServer
    from kanbanger.tools import register_tools

    stub = _StubMCPServer()
    register_tools(stub)
    return stub.tools["setup_project"]


def test_setup_project_tool_scaffolds_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tool = _setup_project_tool(tmp_path, monkeypatch)
    out = tool()

    board = tmp_path / "_kanban.md"
    assert board.exists()
    assert _columns_in(board.read_text(encoding="utf-8")) == CANONICAL_COLUMNS
    assert "Provisioned kanbanger in:" in out
    assert "_kanban.md" in out


def test_setup_project_tool_does_not_clobber_existing_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    board = tmp_path / "_kanban.md"
    sentinel = "# Existing\n\n## BACKLOG\n*   [ ] keep me\n"
    board.write_text(sentinel, encoding="utf-8")

    tool = _setup_project_tool(tmp_path, monkeypatch)
    out = tool()

    assert board.read_text(encoding="utf-8") == sentinel
    assert "already exists" in out or "already present" in out.lower()


def test_setup_project_tool_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tool = _setup_project_tool(tmp_path, monkeypatch)
    tool()
    board_after_first = (tmp_path / "_kanban.md").read_text(encoding="utf-8")
    claude_after_first = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    tool()  # second run

    assert (tmp_path / "_kanban.md").read_text(encoding="utf-8") == board_after_first
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == claude_after_first
    assert claude_after_first.count(CLAUDE_MD_START) == 1


# --- kanbanger init CLI parity ---------------------------------------------


def test_cli_init_provisions_dir(tmp_path: Path):
    from kanbanger.cli import init

    rc = init([str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "_kanban.md").exists()
    assert (tmp_path / "CLAUDE.md").exists()


def test_cli_init_rejects_missing_dir(tmp_path: Path):
    from kanbanger.cli import init

    rc = init([str(tmp_path / "nope")])
    assert rc == 1
