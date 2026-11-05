"""Plugin-only setup must reuse the host's MCP wiring (issue #26).

These tests use real project files and the production FastMCP registration;
no provisioning or tool calls are mocked.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kanban_io import extract_board_key
from kanbanger.cli import init
from kanbanger.provision import (
    CLAUDE_MD_END,
    CLAUDE_MD_START,
    provision_project,
)
from kanbanger.server import create_server


@pytest.fixture(autouse=True)
def clean_install_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KANBANGER_INSTALL_MODE", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)


def _snapshot(project: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(project)): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }


def _call_setup(server, arguments=None) -> str:
    result = asyncio.run(server.call_tool("setup_project", arguments or {}))
    # FastMCP can return content alone or (content, structured_content).
    content = result[0] if isinstance(result, tuple) else result
    return "\n".join(block.text for block in content if hasattr(block, "text"))


def _assert_plugin_guidance(text: str):
    lowered = text.lower()
    assert "plugin" in lowered
    assert "re-enable" in lowered
    assert "setup_project" in text
    assert "Never hand-edit `_kanban.md`" in text
    assert "REVIEW gates DONE" in text
    assert "pipx install" not in lowered
    assert "kanbanger init" not in lowered
    assert "configured here via `.mcp.json`" not in text


def _assert_plugin_summary(summary: str):
    lowered = summary.lower()
    assert "mcp wiring" in lowered
    assert "skipped" in lowered
    assert "plugin" in lowered
    assert "GITHUB_TOKEN" in summary
    assert "no secrets" in lowered
    assert "fill the placeholders in .mcp.json" not in lowered
    assert "targets global" not in lowered


def test_plugin_provision_scaffolds_board_and_hygiene_without_mcp_config(tmp_path: Path):
    result = provision_project(tmp_path, install_mode="plugin")

    assert not (tmp_path / ".mcp.json").exists()
    board = (tmp_path / "_kanban.md").read_text(encoding="utf-8")
    assert extract_board_key(board) is not None
    assert [line[3:] for line in board.splitlines() if line.startswith("## ")] == [
        "BACKLOG", "TODO", "DOING", "REVIEW", "DONE",
    ]
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert all(entry in ignored for entry in (
        ".env", ".env.local", ".kanban.json", ".kanban.lock",
        ".claude/settings.local.json", ".venv/",
    ))
    assert not (tmp_path / "AGENTS.md").exists()
    _assert_plugin_guidance((tmp_path / "CLAUDE.md").read_text(encoding="utf-8"))
    _assert_plugin_summary(result.summary())


@pytest.mark.parametrize("existing", [
    b'{\r\n  "mcpServers": {"other": {"command": "other-server"}}\r\n}\r\n',
    b"user-managed config that is not JSON\r\n",
])
def test_plugin_provision_never_edits_existing_mcp_config(tmp_path: Path, existing: bytes):
    config = tmp_path / ".mcp.json"
    config.write_bytes(existing)

    first = provision_project(tmp_path, install_mode="plugin")
    second = provision_project(tmp_path, install_mode="plugin")

    assert config.read_bytes() == existing
    _assert_plugin_summary(first.summary())
    _assert_plugin_summary(second.summary())
    assert all(".mcp.json" not in note for note in (
        first.created + first.updated + second.created + second.updated
    ))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_plugin_refreshes_only_marker_scoped_guidance_and_is_idempotent(
    tmp_path: Path, newline: str,
):
    prefix = "# Project instructions\n\nKeep this existing guidance.\n\n".replace("\n", newline)
    suffix = "\n\n## Local conventions\nKeep these too.\n".replace("\n", newline)
    stale = (
        prefix + CLAUDE_MD_START + "\nInstall globally with pipx install old.\n"
        + CLAUDE_MD_END + suffix
    )
    for name in ("CLAUDE.md", "AGENTS.md"):
        (tmp_path / name).write_bytes(stale.encode("utf-8"))

    provision_project(tmp_path, install_mode="plugin")
    first = _snapshot(tmp_path)
    first_key = extract_board_key((tmp_path / "_kanban.md").read_text(encoding="utf-8"))
    result = provision_project(tmp_path, install_mode="plugin")

    assert first_key is not None
    assert _snapshot(tmp_path) == first
    assert result.created == []
    assert result.updated == []
    assert extract_board_key((tmp_path / "_kanban.md").read_text(encoding="utf-8")) == first_key
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = (tmp_path / name).read_bytes().decode("utf-8")
        assert text.startswith(prefix)
        assert text.endswith(suffix)
        assert text.count(CLAUDE_MD_START) == 1
        assert text.count(CLAUDE_MD_END) == 1
        _assert_plugin_guidance(text)


@pytest.mark.parametrize("existing_block", [False, True], ids=["append", "refresh"])
def test_plugin_setup_preserves_symlinked_guidance_and_updates_referents(
    tmp_path: Path, existing_block: bool,
):
    project = tmp_path / "project"
    shared = tmp_path / "shared-guidance"
    project.mkdir()
    shared.mkdir()
    prefix = b"# Shared instructions\r\n\r\nPreserve this custom guidance.\r\n"
    suffix = b"\r\n## Team conventions\r\nPreserve these too.\r\n"
    managed = (
        CLAUDE_MD_START.encode("utf-8") + b"\r\nStale Kanbanger guidance.\r\n"
        + CLAUDE_MD_END.encode("utf-8")
    ) if existing_block else b""
    original = prefix + managed + suffix
    targets = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        target = shared / name
        target.write_bytes(original)
        try:
            (project / name).symlink_to(target)
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"OS cannot create guidance symlinks: {error}")
        targets.append(target)

    provision_project(project, install_mode="plugin")

    for target in targets:
        link = project / target.name
        assert link.is_symlink(), f"Provisioning replaced the {target.name} symlink"
        assert link.resolve() == target.resolve()
        updated = target.read_bytes()
        assert updated.startswith(prefix)
        if existing_block:
            assert updated.endswith(suffix)
        else:
            assert updated.startswith(original)
        assert updated.count(CLAUDE_MD_START.encode("utf-8")) == 1
        _assert_plugin_guidance(updated.decode("utf-8"))

    first = {target: target.read_bytes() for target in targets}
    provision_project(project, install_mode="plugin")
    for target in targets:
        assert (project / target.name).is_symlink()
        assert target.read_bytes() == first[target]


@pytest.mark.parametrize("entrypoint", ["provision", "cli"])
def test_standalone_entrypoints_ignore_ambient_plugin_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str,
):
    monkeypatch.setenv("KANBANGER_INSTALL_MODE", "plugin")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin-cache"))

    if entrypoint == "cli":
        assert init([str(tmp_path)]) == 0
    else:
        provision_project(tmp_path)

    config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["kanbanger"]["command"] == "kanbanger-mcp"
    assert config["mcpServers"]["kanbanger"]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN:-}"
    guidance = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "pipx install" in guidance
    assert "kanbanger init" in guidance


@pytest.mark.parametrize("invalid", ["automatic", "PLUGIN", ""])
def test_core_invalid_install_mode_is_rejected_before_any_writes(tmp_path: Path, invalid: str):
    (tmp_path / "AGENTS.md").write_text("Keep my instructions.\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    with pytest.raises(ValueError, match="install_mode"):
        provision_project(tmp_path, install_mode=invalid)

    assert _snapshot(tmp_path) == before


def test_mcp_marker_setup_reuses_plugin_wiring_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("KANBANGER_INSTALL_MODE", "plugin")
    (tmp_path / "AGENTS.md").write_text("# Existing agent instructions\n", encoding="utf-8")
    server = create_server()

    summary = _call_setup(server)

    assert not (tmp_path / ".mcp.json").exists()
    assert extract_board_key((tmp_path / "_kanban.md").read_text(encoding="utf-8")) is not None
    _assert_plugin_summary(summary)
    for name in ("CLAUDE.md", "AGENTS.md"):
        _assert_plugin_guidance((tmp_path / name).read_text(encoding="utf-8"))
    first = _snapshot(tmp_path)

    _assert_plugin_summary(_call_setup(server))
    assert _snapshot(tmp_path) == first


def test_mcp_plugin_setup_preserves_other_server_config_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("KANBANGER_INSTALL_MODE", "plugin")
    original = b'{"mcpServers":{"other":{"command":"keep-me"}}}\r\n'
    (tmp_path / ".mcp.json").write_bytes(original)

    summary = _call_setup(create_server())

    assert (tmp_path / ".mcp.json").read_bytes() == original
    _assert_plugin_summary(summary)
    _assert_plugin_guidance((tmp_path / "CLAUDE.md").read_text(encoding="utf-8"))


@pytest.mark.parametrize("marker", [None, "standalone"])
def test_mcp_unmarked_or_standalone_launch_keeps_standalone_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker,
):
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    if marker is not None:
        monkeypatch.setenv("KANBANGER_INSTALL_MODE", marker)

    summary = _call_setup(create_server())

    config = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert config["mcpServers"]["kanbanger"]["command"] == "kanbanger-mcp"
    assert "pipx install" in (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Provisioned kanbanger in:" in summary


@pytest.mark.parametrize("explicit,marker", [
    ("plugin", "standalone"),
    ("standalone", "plugin"),
    ("plugin", "invalid-ambient-marker"),
])
def test_mcp_explicit_install_mode_overrides_launch_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: str, marker: str,
):
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("KANBANGER_INSTALL_MODE", marker)

    summary = _call_setup(create_server(), {"install_mode": explicit})

    assert "Provisioned kanbanger in:" in summary
    assert (tmp_path / ".mcp.json").exists() == (explicit == "standalone")
    guidance = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    if explicit == "plugin":
        _assert_plugin_summary(summary)
        _assert_plugin_guidance(guidance)
    else:
        assert "pipx install" in guidance


@pytest.mark.parametrize("source,invalid", [
    ("argument", "automatic"), ("argument", ""),
    ("environment", "automatic"), ("environment", ""),
])
def test_mcp_invalid_install_mode_returns_configuration_error_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, invalid: str,
):
    monkeypatch.setenv("KANBANGER_WORKSPACE", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("Keep my instructions.\n", encoding="utf-8")
    before = _snapshot(tmp_path)
    arguments = {}
    if source == "argument":
        arguments["install_mode"] = invalid
    else:
        monkeypatch.setenv("KANBANGER_INSTALL_MODE", invalid)

    payload = json.loads(_call_setup(create_server(), arguments))

    assert payload["success"] is False
    assert payload["error_code"] == "configuration_error"
    assert "install_mode" in payload["message"]
    assert _snapshot(tmp_path) == before


def test_mcp_install_mode_is_optional_on_existing_eleven_tool_surface():
    tools = asyncio.run(create_server().list_tools())
    assert len(tools) == 11
    setup = next(tool for tool in tools if tool.name == "setup_project")
    assert "install_mode" in setup.inputSchema["properties"]
    assert "install_mode" not in setup.inputSchema.get("required", [])
