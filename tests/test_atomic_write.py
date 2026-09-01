import sys

import pytest

import kanban_io


def _tempfiles(target):
    return list(target.parent.glob(f"{target.name}.tmp.*"))


def test_retries_transient_windows_permission_error(tmp_path, monkeypatch):
    target = tmp_path / "board.md"
    target.write_text("old", encoding="utf-8")
    real_replace = kanban_io.os.replace
    attempts = 0
    sleeps = []

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(kanban_io.sys, "platform", "win32")
    monkeypatch.setattr(kanban_io.os, "replace", flaky_replace)
    monkeypatch.setattr(kanban_io.time, "sleep", sleeps.append)

    kanban_io.atomic_write_text(str(target), "new")

    assert attempts == 3
    assert sleeps == [0.05, 0.05]
    assert target.read_text(encoding="utf-8") == "new"
    assert _tempfiles(target) == []


def test_preserves_permission_error_after_windows_retry_budget(
    tmp_path, monkeypatch
):
    target = tmp_path / "board.md"
    target.write_text("old", encoding="utf-8")
    failure = PermissionError("still busy")
    attempts = 0
    sleeps = []

    def always_fail(source, destination):
        nonlocal attempts
        attempts += 1
        raise failure

    monkeypatch.setattr(kanban_io.sys, "platform", "win32")
    monkeypatch.setattr(kanban_io.os, "replace", always_fail)
    monkeypatch.setattr(kanban_io.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError) as exc_info:
        kanban_io.atomic_write_text(str(target), "new")

    assert exc_info.value is failure
    assert attempts == 5
    assert sleeps == [0.05] * 4
    assert target.read_text(encoding="utf-8") == "old"
    assert _tempfiles(target) == []


@pytest.mark.parametrize(
    ("platform", "failure"),
    [
        ("linux", PermissionError("not Windows")),
        ("win32", OSError("not a sharing violation")),
    ],
)
def test_does_not_retry_unqualified_errors(
    tmp_path, monkeypatch, platform, failure
):
    target = tmp_path / "board.md"
    target.write_text("old", encoding="utf-8")
    attempts = 0
    sleeps = []

    def fail(source, destination):
        nonlocal attempts
        attempts += 1
        raise failure

    monkeypatch.setattr(kanban_io.sys, "platform", platform)
    monkeypatch.setattr(kanban_io.os, "replace", fail)
    monkeypatch.setattr(kanban_io.time, "sleep", sleeps.append)

    with pytest.raises(type(failure)) as exc_info:
        kanban_io.atomic_write_text(str(target), "new")

    assert exc_info.value is failure
    assert attempts == 1
    assert sleeps == []
    assert target.read_text(encoding="utf-8") == "old"
    assert _tempfiles(target) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")
def test_real_windows_reader_sharing_violation_is_retried(
    tmp_path, monkeypatch
):
    target = tmp_path / "board.md"
    target.write_text("old", encoding="utf-8")
    reader = target.open("r", encoding="utf-8")
    real_replace = kanban_io.os.replace
    sharing_failures = 0

    def release_reader_after_real_failure(source, destination):
        nonlocal sharing_failures
        try:
            real_replace(source, destination)
        except PermissionError:
            sharing_failures += 1
            reader.close()
            raise

    monkeypatch.setattr(
        kanban_io.os, "replace", release_reader_after_real_failure
    )
    monkeypatch.setattr(kanban_io.time, "sleep", lambda _delay: None)

    try:
        kanban_io.atomic_write_text(str(target), "new")
    finally:
        reader.close()

    assert sharing_failures == 1
    assert target.read_text(encoding="utf-8") == "new"
    assert _tempfiles(target) == []
