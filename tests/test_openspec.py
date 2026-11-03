from __future__ import annotations

import subprocess
from pathlib import Path

from dev_env.openspec import setup_openspec


def _completed(stdout: str = "OpenSpec setup complete", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_init_uses_non_interactive_opencode_target_and_initializes_project(tmp_path: Path, monkeypatch):
    executable = "/usr/local/bin/openspec"
    monkeypatch.setattr("dev_env.openspec.find_openspec", lambda: executable)
    monkeypatch.setattr("dev_env.openspec.openspec_version", lambda _path: "OpenSpec v1.7.0")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        config = tmp_path / "openspec" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("schema: spec-driven\n", encoding="utf-8")
        return _completed()

    result = setup_openspec(tmp_path, runner=runner)

    assert calls[0][0] == [executable, "init", "--tools", "opencode"]
    assert calls[0][1]["cwd"] == tmp_path
    assert result.status == "initialized"
    assert result.initialized is True
    assert result.version == "OpenSpec v1.7.0"


def test_update_only_runs_for_initialized_project(tmp_path: Path, monkeypatch):
    executable = "/usr/local/bin/openspec"
    monkeypatch.setattr("dev_env.openspec.find_openspec", lambda: executable)
    monkeypatch.setattr("dev_env.openspec.openspec_version", lambda _path: "OpenSpec v1.7.0")
    calls = []
    result = setup_openspec(tmp_path, mode="update", runner=lambda command, **kwargs: calls.append(command))

    assert result.status == "not-initialized"
    assert calls == []


def test_update_sets_no_update_check_environment(tmp_path: Path, monkeypatch):
    executable = "/usr/local/bin/openspec"
    monkeypatch.setattr("dev_env.openspec.find_openspec", lambda: executable)
    monkeypatch.setattr("dev_env.openspec.openspec_version", lambda _path: "OpenSpec v1.7.0")
    config = tmp_path / "openspec" / "config.yaml"
    config.parent.mkdir()
    config.write_text("schema: spec-driven\n", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return _completed("Updated")

    result = setup_openspec(tmp_path, mode="update", runner=runner)

    assert calls[0][0] == [executable, "update"]
    assert calls[0][1]["env"]["OPENSPEC_NO_UPDATE_CHECK"] == "1"
    assert result.updated is True


def test_skip_does_not_detect_or_run_openspec(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "dev_env.openspec.find_openspec",
        lambda: (_ for _ in ()).throw(AssertionError("must not detect when skipped")),
    )
    result = setup_openspec(tmp_path, skip=True)
    assert result.status == "skipped"


def test_declined_install_does_not_run_project_init(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("dev_env.openspec.find_openspec", lambda: None)
    monkeypatch.setattr("dev_env.openspec.sys.stdin.isatty", lambda: True)
    calls = []
    result = setup_openspec(
        tmp_path,
        input_fn=lambda _prompt: "n",
        runner=lambda command, **kwargs: calls.append(command),
    )

    assert result.status == "not-installed"
    assert result.install_declined is True
    assert calls == []


def test_approved_install_runs_installer_then_project_init(tmp_path: Path, monkeypatch):
    executable = "/home/test/.local/bin/openspec"
    detected = iter([None, executable])
    monkeypatch.setattr("dev_env.openspec.find_openspec", lambda: next(detected))
    monkeypatch.setattr("dev_env.openspec.install_openspec", lambda: "Installed")
    monkeypatch.setattr("dev_env.openspec.openspec_version", lambda _path: "OpenSpec v1.7.0")
    monkeypatch.setattr("dev_env.openspec.sys.stdin.isatty", lambda: True)

    def runner(command, **kwargs):
        config = tmp_path / "openspec" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("schema: spec-driven\n", encoding="utf-8")
        return _completed()

    result = setup_openspec(tmp_path, input_fn=lambda _prompt: "yes", runner=runner)

    assert result.initialized is True
    assert "установлен OpenSpec CLI (standalone)" in result.actions
    assert "Installed" in result.output
