from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

from dev_env.manifest import save_manifest
from dev_env.python_tools import (
    DEFAULT_TOOLS,
    PYTHON_EXCLUDES,
    _ensure_mise_config,
    _choose_python_version,
    _requirements_install_command,
    _run_mise_bootstrap,
    _run_mise_install,
    _tool_install_candidates,
    detect_virtual_environment,
    setup_python_tooling,
)


def make_environment(path: Path, version: str = "3.12.7") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    binary_dir = path / ("Scripts" if os.name == "nt" else "bin")
    binary_dir.mkdir(exist_ok=True)
    executable = binary_dir / ("python.exe" if os.name == "nt" else "python")
    if os.name == "nt":
        executable.write_text(f"@echo Python {version}\n", encoding="utf-8")
    else:
        executable.write_text(f"#!/bin/sh\necho 'Python {version}'\n", encoding="utf-8")
        executable.chmod(0o755)
    return executable


def make_git_project(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def make_manifest(root: Path) -> None:
    save_manifest(
        root,
        {
            "manifest_version": 1,
            "template": {"name": "test", "version": "1.0.0", "revision": None},
            "updated_at": "2026-11-03T00:00:00+00:00",
            "files": {},
            "exclude_patterns": ["/AGENTS.md", "/opencode.json", "/.opencode/", "/openspec/", "/.gitignore", "/.dev-env/"],
        },
    )



def test_python_version_prompt_uses_detected_default_and_accepts_selection(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    selected = _choose_python_version(
        project, None, interactive=True, input_fn=lambda prompt: "3.13"
    )

    assert selected == "3.13"


def test_python_version_prompt_keeps_detected_version_on_empty_input(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    selected = _choose_python_version(
        project, None, interactive=True, input_fn=lambda prompt: ""
    )

    assert selected == "3.12"


def test_python_version_prompt_uses_existing_mise_choice_as_default_and_allows_change(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text('[tools]\npython = "3.11"\n', encoding="utf-8")
    prompts: list[str] = []

    def choose(prompt: str) -> str:
        prompts.append(prompt)
        return "3.13"

    selected = _choose_python_version(project, None, interactive=True, input_fn=choose)

    assert selected == "3.13"
    assert len(prompts) == 1
    assert "[3.11]" in prompts[0]


def test_python_version_prompt_keeps_existing_mise_choice_on_empty_input(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text('[tools]\npython = "3.11"\n', encoding="utf-8")

    selected = _choose_python_version(project, None, interactive=True, input_fn=lambda prompt: "")

    assert selected == "3.11"


def test_setup_prompts_and_updates_existing_mise_python_version(tmp_path: Path, monkeypatch) -> None:
    project = make_git_project(tmp_path / "project")
    (project / "mise.toml").write_text(
        '[tools]\npython = "3.12"\nuv = "latest"\n"pypi:ruff" = "latest"\n'
        'node = "24"\n"npm:pyright" = "latest"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("dev_env.python_tools.sys.stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("dev_env.python_tools._mise_executable", lambda: "/usr/bin/mise")
    monkeypatch.setattr("dev_env.python_tools._tmux_bootstrap_package", lambda: None)
    monkeypatch.setattr("dev_env.python_tools._tool_install_candidates", lambda *args, **kwargs: [])
    prompts: list[str] = []

    def choose(prompt: str) -> str:
        prompts.append(prompt)
        return "3.13"

    result = setup_python_tooling(project, install_missing=True, input_fn=choose)

    config = tomllib.loads((project / "mise.toml").read_text(encoding="utf-8"))
    assert result.python_version == "3.13"
    assert config["tools"]["python"] == "3.13"
    assert len(prompts) == 1
    assert "[3.12]" in prompts[0]


def test_python_version_override_updates_existing_mise_setting_and_preserves_comments(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text(
        '[tools]\npython = "3.11" # existing choice\nnode = "24"\n', encoding="utf-8"
    )

    _created, updated, config = _ensure_mise_config(
        project, environment=None, python_version_override="3.13"
    )

    content = (project / "mise.toml").read_text(encoding="utf-8")
    assert config["tools"]["python"] == "3.13"
    assert 'python = "3.13" # existing choice' in content
    assert 'node = "24"' in content
    assert any("версия Python изменена на 3.13" in item for item in updated)


def test_python_version_prompt_is_skipped_in_noninteractive_mode(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    selected = _choose_python_version(
        project, None, interactive=False, input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError("prompt must not run"))
    )

    assert selected == "3.12"

def test_detect_project_local_venv_before_unrelated_active_venv(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    local_python = make_environment(project / ".venv", "3.12.7")
    external_python = make_environment(tmp_path / "other-project" / ".venv", "3.13.1")

    detected = detect_virtual_environment(
        project,
        environ={"VIRTUAL_ENV": str(external_python.parent.parent)},
    )

    assert detected is not None
    assert detected.path == project / ".venv"
    assert detected.python == local_python
    assert detected.version == "3.12.7"
    assert detected.is_project_local is True


def test_detect_explicit_uv_project_environment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    python = make_environment(project / "custom-env", "3.11.9")

    detected = detect_virtual_environment(
        project,
        environ={"UV_PROJECT_ENVIRONMENT": "custom-env"},
    )

    assert detected is not None
    assert detected.path == project / "custom-env"
    assert detected.python == python
    assert detected.source == "UV_PROJECT_ENVIRONMENT"


def test_mise_config_is_valid_and_uses_relative_paths_for_requirements_project(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    created, updated, config = _ensure_mise_config(project, environment=None)
    content = (project / "mise.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(content)

    assert created == ["mise.toml"]
    assert not updated
    assert set(DEFAULT_TOOLS).issubset(parsed["tools"])
    assert parsed["tools"]["npm:opencode-ai"] == {
        "version": "latest",
        "allow_builds": ["opencode-ai"],
    }
    assert {"install", "lint", "format", "typecheck", "test", "check", "opencode", "serve"}.issubset(parsed["tasks"])
    assert parsed["tasks"]["serve"]["run"] == (
        'PATH="$HOME/.local/bin:$PATH" '
        "opencode serve --port ${OPENCODE_PORT:-4096} --hostname 127.0.0.1"
    )
    assert parsed["tasks"]["opencode"]["interactive"] is True
    assert 'export PATH="$HOME/.local/bin:$PATH"' in parsed["tasks"]["opencode"]["run"]
    assert "TMUX" in parsed["tasks"]["opencode"]["run"]
    assert "tmux new-session -A" in parsed["tasks"]["opencode"]["run"]
    assert parsed["bootstrap"]["packages"]["apt:tmux"] == "latest"
    assert parsed["settings"]["python"]["uv_venv_auto"] == "source"
    assert parsed["env"]["_"]["python"]["venv"] == {"path": ".venv", "create": False}
    assert parsed["tasks"]["install"]["run"] == (
        "uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt"
    )
    assert str(project) not in content
    assert (project / ".venv").exists() is False  # init never creates the environment



def test_mise_config_adds_tmux_bootstrap_for_macos(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("dev_env.python_tools.platform.system", lambda: "Darwin")
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    _ensure_mise_config(project, environment=None)
    parsed = tomllib.loads((project / "mise.toml").read_text(encoding="utf-8"))

    assert parsed["bootstrap"]["packages"]["brew:tmux"] == "latest"


def test_existing_opencode_task_is_preserved(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text(
        '[tasks.opencode]\ndescription = "My launcher"\nrun = "echo custom"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    _ensure_mise_config(project, environment=None)
    parsed = tomllib.loads((project / "mise.toml").read_text(encoding="utf-8"))

    assert parsed["tasks"]["opencode"]["run"] == "echo custom"
    assert parsed["tasks"]["opencode"]["description"] == "My launcher"


def test_run_mise_bootstrap_trusts_project_then_runs_bootstrap(tmp_path: Path, monkeypatch) -> None:
    from subprocess import CompletedProcess

    calls = []
    monkeypatch.setattr("dev_env.python_tools._mise_executable", lambda: "/usr/bin/mise")
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: "/usr/bin/tmux" if command == "tmux" else None)

    def fake_run(command, **kwargs):
        calls.append(command)
        return CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("dev_env.python_tools.subprocess.run", fake_run)

    success, detail = _run_mise_bootstrap(tmp_path, "apt:tmux")

    assert success is True
    assert detail == "ok"
    assert calls == [
        ["/usr/bin/mise", "bootstrap", "packages", "use", "apt:tmux"],
    ]

def test_uv_project_task_uses_uv_sync_and_source_only_venv(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    (project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    _ensure_mise_config(project, environment=None)
    parsed = tomllib.loads((project / "mise.toml").read_text(encoding="utf-8"))

    assert parsed["tasks"]["install"]["run"] == "uv sync --locked"
    assert parsed["settings"]["python"]["uv_venv_auto"] == "source"
    assert "_" not in parsed.get("env", {})


def test_existing_mise_values_and_tasks_are_preserved(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text(
        '[tools]\npython = "3.13"\n\n[tasks.lint]\nrun = "ruff check src --output-format concise"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    _ensure_mise_config(project, environment=None)
    parsed = tomllib.loads((project / "mise.toml").read_text(encoding="utf-8"))

    assert parsed["tools"]["python"] == "3.13"
    assert parsed["tasks"]["lint"]["run"] == "ruff check src --output-format concise"
    assert "format" in parsed["tasks"]


def test_requirements_install_reuses_existing_named_environment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    python = make_environment(project / "venv", "3.12.7")
    detected = detect_virtual_environment(project, environ={})

    command = _requirements_install_command(project, detected)

    assert "uv venv" not in command
    assert command == "uv pip install --python venv/bin/python -r requirements.txt"
    assert str(project) not in command
    assert python.is_file()


def test_setup_preserves_template_git_excludes_when_adding_python_excludes(tmp_path: Path, monkeypatch) -> None:
    project = make_git_project(tmp_path / "project")
    make_manifest(project)
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: {
        "uv": "uv 0.9.0", "ruff": "ruff 0.14.0", "node": "v24.0.0", "pyright": "pyright 1.1.400", "mise": "mise 2026.11.0"
    }.get(command))
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: f"/usr/bin/{command}")

    result = setup_python_tooling(project, install_missing=False)

    exclude = (project / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    for pattern in ("/AGENTS.md", "/opencode.json", "/.opencode/", "/openspec/", "/.gitignore", *PYTHON_EXCLUDES):
        assert pattern in exclude
    assert "mise.toml" in result.created
    assert (project / "mise.toml").is_file()


def test_tool_install_candidates_skip_healthy_globally_available_tools(tmp_path: Path, monkeypatch) -> None:
    config = {
        "tools": {
            "python": "3.12",
            "uv": "latest",
            "pypi:ruff": "latest",
            "node": "24",
            "npm:pyright": "latest",
        }
    }
    versions = {
        "uv": "uv 0.9.0",
        "ruff": "ruff 0.14.0",
        "node": "v24.1.0",
        "pyright": "pyright 1.1.400",
    }
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: versions.get(command))
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        "dev_env.python_tools._mise_which",
        lambda command, **kwargs: f"/mise/installs/{command}/bin/{command}" if command in {"python", "uv"} else None,
    )

    assert _tool_install_candidates(tmp_path, config, None) == []


def test_tool_install_candidates_include_declared_opencode(tmp_path: Path, monkeypatch) -> None:
    config = {
        "tools": {
            "python": "3.12",
            "uv": "latest",
            "pypi:ruff": "latest",
            "node": "24",
            "npm:pyright": "latest",
            "npm:opencode-ai": {"version": "latest", "allow_builds": ["opencode-ai"]},
        }
    }
    versions = {
        "uv": "uv 0.9.0",
        "ruff": "ruff 0.14.0",
        "node": "v24.1.0",
        "pyright": "pyright 1.1.400",
    }
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: versions.get(command))
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        "dev_env.python_tools._mise_which",
        lambda command, **kwargs: f"/mise/installs/{command}/bin/{command}" if command in {"python", "uv"} else None,
    )

    assert _tool_install_candidates(tmp_path, config, None) == [
        "node@24",
        "npm:opencode-ai@latest",
    ]


def test_run_mise_install_reports_lock_failure_as_warning(tmp_path: Path, monkeypatch) -> None:
    from subprocess import CompletedProcess

    monkeypatch.setattr("dev_env.python_tools._mise_executable", lambda: "/usr/bin/mise")

    def fake_run(command, **kwargs):
        if command[1] == "lock":
            return CompletedProcess(command, 1, stdout="lock failed\n", stderr="")
        return CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("dev_env.python_tools.subprocess.run", fake_run)

    installed, failures, warnings = _run_mise_install(tmp_path, ["npm:opencode-ai@latest"])

    assert installed == ["npm:opencode-ai@latest"]
    assert failures == []
    assert warnings == ["mise lock: lock failed"]


def test_missing_pypi_tool_also_installs_mise_managed_python_first(
    tmp_path: Path, monkeypatch
) -> None:
    config = {
        "tools": {
            "python": "3.12",
            "uv": "latest",
            "pypi:ruff": "latest",
            "node": "24",
            "npm:pyright": "latest",
        }
    }
    versions = {
        "uv": "uv 0.9.0",
        # Ruff is missing; the system/venv Python is present and compatible.
        "node": "v24.1.0",
        "pyright": "pyright 1.1.400",
    }
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: versions.get(command))
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12.3")
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr("dev_env.python_tools._mise_which", lambda command, **kwargs: None)

    assert _tool_install_candidates(tmp_path, config, None) == [
        "python@3.12",
        "uv@latest",
        "pypi:ruff@latest",
    ]


def test_missing_pypi_tool_installs_mise_uv_even_when_system_uv_exists(
    tmp_path: Path, monkeypatch
) -> None:
    config = {"tools": {"python": "3.12", "uv": "latest", "pypi:ruff": "latest"}}
    versions = {"uv": "uv 0.12.17", "ruff": None, "node": "v24.1.0", "pyright": "pyright 1.1.414"}
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: versions.get(command))
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12.16")
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        "dev_env.python_tools._mise_which",
        lambda command, **kwargs: "/mise/installs/python/3.12.16/bin/python" if command == "python" else None,
    )

    assert _tool_install_candidates(tmp_path, config, None) == [
        "uv@latest",
        "pypi:ruff@latest",
    ]


def test_missing_npm_tool_also_installs_mise_managed_node_first(
    tmp_path: Path, monkeypatch
) -> None:
    config = {
        "tools": {
            "python": "3.12",
            "uv": "latest",
            "pypi:ruff": "latest",
            "node": "24",
            "npm:pyright": "latest",
        }
    }
    versions = {
        "uv": "uv 0.9.0",
        "ruff": "ruff 0.14.0",
        # Pyright is missing; system Node.js is present and compatible.
    }
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: versions.get(command))
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12.3")
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr("dev_env.python_tools._resolve_command_executable", lambda command, **kwargs: f"/usr/bin/{command}")

    assert _tool_install_candidates(tmp_path, config, None) == [
        "node@24",
        "npm:pyright@latest",
    ]


def test_setup_does_not_overwrite_existing_ruff_or_pyright_config(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    custom_ruff = "line-length = 88\n"
    custom_pyright = json.dumps({"typeCheckingMode": "strict"})
    (project / "ruff.toml").write_text(custom_ruff, encoding="utf-8")
    (project / "pyrightconfig.json").write_text(custom_pyright, encoding="utf-8")
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    _ensure_mise_config(project, environment=None)
    from dev_env.python_tools import _prepare_tool_configs
    _prepare_tool_configs(project, "3.12")

    assert (project / "ruff.toml").read_text(encoding="utf-8") == custom_ruff
    assert (project / "pyrightconfig.json").read_text(encoding="utf-8") == custom_pyright


def test_confirm_install_requires_explicit_yes_when_not_assume_yes(monkeypatch) -> None:
    from dev_env.python_tools import _confirm_install

    monkeypatch.setattr("dev_env.python_tools.sys.stdin.isatty", lambda: True)
    assert _confirm_install(["ruff@latest"], assume_yes=False, input_fn=lambda _: "n") is False
    assert _confirm_install(["ruff@latest"], assume_yes=False, input_fn=lambda _: "y") is True
    assert _confirm_install(["ruff@latest"], assume_yes=True, input_fn=lambda _: "n") is True


def test_requirements_install_creates_venv_only_when_none_exists(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    command = _requirements_install_command(project, None)

    assert command == (
        "uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt"
    )


def test_existing_nested_mise_python_settings_are_extended_without_toml_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text(
        '[settings.python]\nvenv_create_args = ["--upgrade-deps"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")

    _ensure_mise_config(project, environment=None)
    parsed = tomllib.loads((project / "mise.toml").read_text(encoding="utf-8"))

    assert parsed["settings"]["python"]["venv_create_args"] == ["--upgrade-deps"]
    assert parsed["settings"]["python"]["uv_venv_auto"] == "source"


def test_install_candidates_include_node_when_project_selector_is_not_satisfied(
    tmp_path: Path, monkeypatch
) -> None:
    config = {
        "tools": {
            "python": "3.12",
            "uv": "latest",
            "pypi:ruff": "latest",
            "node": "24",
            "npm:pyright": "latest",
        }
    }
    versions = {
        "uv": "uv 0.9.0",
        "ruff": "ruff 0.14.0",
        "node": "v22.1.0",
        "pyright": "pyright 1.1.400",
    }
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: versions.get(command))
    monkeypatch.setattr("dev_env.python_tools._system_python_version", lambda *_: "3.12")
    monkeypatch.setattr("dev_env.python_tools._resolve_command_executable", lambda command, **kwargs: f"/usr/bin/{command}")

    assert _tool_install_candidates(tmp_path, config, None) == ["node@24"]


def test_detector_prefers_project_selected_mise_binary(tmp_path: Path, monkeypatch) -> None:
    from subprocess import CompletedProcess

    from dev_env.python_tools import _resolve_command_executable

    expected = "/mise/installs/ruff/0.14.0/bin/ruff"
    monkeypatch.setattr("dev_env.python_tools._mise_executable", lambda: "/usr/bin/mise")

    def fake_run(command, **kwargs):
        assert command == ["/usr/bin/mise", "which", "ruff"]
        return CompletedProcess(command, 0, stdout=expected + "\n", stderr="")

    monkeypatch.setattr("dev_env.python_tools.subprocess.run", fake_run)
    monkeypatch.setattr("dev_env.python_tools.Path.is_file", lambda self: str(self) == expected)
    monkeypatch.setattr("dev_env.python_tools.os.access", lambda path, mode: str(path) == expected)
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: "/usr/bin/ruff")

    assert _resolve_command_executable("ruff", cwd=tmp_path) == expected


def test_doctor_reports_python_tool_version_mismatches(tmp_path: Path, monkeypatch) -> None:
    from dev_env.python_tools import diagnose_python_tooling

    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text(
        '[tools]\npython = "3.12"\nuv = "latest"\n"pypi:ruff" = "latest"\n'
        'node = "24"\n"npm:pyright" = "latest"\n\n'
        '[tasks.install]\nrun = "uv sync"\n',
        encoding="utf-8",
    )
    (project / "ruff.toml").write_text('line-length = 100\n', encoding="utf-8")
    (project / "pyrightconfig.json").write_text('{"typeCheckingMode":"basic"}\n', encoding="utf-8")
    versions = {
        "mise": "mise 2026.11.0",
        "uv": "uv 0.9.0",
        "ruff": "ruff 0.14.0",
        "node": "v22.1.0",
        "pyright": "pyright 1.1.400",
    }
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: versions.get(command))
    monkeypatch.setattr("dev_env.python_tools._mise_executable", lambda: "/usr/bin/mise")
    monkeypatch.setattr("dev_env.python_tools.detect_virtual_environment", lambda root: None)
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr("dev_env.python_tools._version_from_python", lambda executable: "3.12.5")
    monkeypatch.setattr("dev_env.python_tools._resolve_command_executable", lambda command, **kwargs: f"/usr/bin/{command}")
    monkeypatch.setattr(
        "dev_env.python_tools._mise_which",
        lambda command, **kwargs: "/mise/installs/npm-opencode-ai/1.19.5/bin/opencode" if command == "opencode" else None,
    )

    checks = diagnose_python_tooling(project)
    by_name = {check.name: check for check in checks}

    assert by_name["Node.js"].status == "WARN"
    assert "ожидается 24" in by_name["Node.js"].detail
    assert by_name["Python"].status == "OK"
    assert by_name["Pyright language server"].status == "OK"
    assert by_name["opencode"].status == "OK"


def test_doctor_warns_when_opencode_is_not_installed_through_mise(
    tmp_path: Path, monkeypatch
) -> None:
    from dev_env.python_tools import diagnose_python_tooling

    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text(
        '[tools]\npython = "3.12"\nuv = "latest"\n"pypi:ruff" = "latest"\n'
        'node = "24"\n"npm:pyright" = "latest"\n'
        '"npm:opencode-ai" = { version = "latest", allow_builds = ["opencode-ai"] }\n',
        encoding="utf-8",
    )
    versions = {
        "mise": "mise 2026.11.0",
        "uv": "uv 0.9.0",
        "ruff": "ruff 0.14.0",
        "node": "v24.1.0",
        "pyright": "pyright 1.1.400",
    }
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: versions.get(command))
    monkeypatch.setattr("dev_env.python_tools._mise_executable", lambda: "/usr/bin/mise")
    monkeypatch.setattr("dev_env.python_tools._mise_which", lambda command, **kwargs: None)
    monkeypatch.setattr("dev_env.python_tools.detect_virtual_environment", lambda root: None)
    monkeypatch.setattr("dev_env.python_tools.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr("dev_env.python_tools._version_from_python", lambda executable: "3.12.5")
    monkeypatch.setattr("dev_env.python_tools._resolve_command_executable", lambda command, **kwargs: f"/usr/bin/{command}")

    checks = diagnose_python_tooling(project)
    opencode = next(check for check in checks if check.name == "opencode")

    assert opencode.status == "WARN"
    assert "mise install" in opencode.detail


def test_doctor_uses_mise_selected_python_when_shell_shims_are_not_active(
    tmp_path: Path, monkeypatch
) -> None:
    from dev_env.python_tools import diagnose_python_tooling

    project = tmp_path / "project"
    project.mkdir()
    (project / "mise.toml").write_text(
        '[tools]\npython = "3.12"\nuv = "latest"\n"pypi:ruff" = "latest"\n'
        'node = "24"\n"npm:pyright" = "latest"\n',
        encoding="utf-8",
    )
    selected_python = "/mise/installs/python/3.12.7/bin/python"
    monkeypatch.setattr("dev_env.python_tools._command_version", lambda command, **kwargs: {
        "mise": "mise 2026.11.0",
        "uv": "uv 0.9.0",
        "ruff": "ruff 0.14.0",
        "node": "v24.1.0",
        "pyright": "pyright 1.1.400",
    }.get(command))
    monkeypatch.setattr("dev_env.python_tools._mise_executable", lambda: "/usr/bin/mise")
    monkeypatch.setattr(
        "dev_env.python_tools._resolve_command_executable",
        lambda command, **kwargs: (selected_python if command == "python" else f"/usr/bin/{command}"),
    )
    monkeypatch.setattr("dev_env.python_tools.detect_virtual_environment", lambda root: None)
    monkeypatch.setattr("dev_env.python_tools._version_from_python", lambda executable: "3.12.7" if executable == selected_python else None)
    monkeypatch.setattr("dev_env.python_tools._mise_which", lambda command, **kwargs: None)

    checks = diagnose_python_tooling(project)
    python_check = next(check for check in checks if check.name == "Python")

    assert python_check.status == "OK"
    assert selected_python in python_check.detail
