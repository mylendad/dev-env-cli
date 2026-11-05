from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .errors import DevEnvError
from .git import update_exclude
from .manifest import load_manifest, save_manifest

DEFAULT_PYTHON_VERSION = "3.12"
DEFAULT_TOOLS = {
    "python": DEFAULT_PYTHON_VERSION,
    "uv": "latest",
    "pypi:ruff": "latest",
    "node": "24",
    "npm:pyright": "latest",
    "npm:opencode-ai": "latest",
}

PYTHON_EXCLUDES = (
    "/.venv/",
    "/venv/",
    "/env/",
    "/__pycache__/",
    "/.ruff_cache/",
    "/.pytest_cache/",
    "/.pyright/",
    "/.mypy_cache/",
)

RUFF_CONFIG_NAME = "ruff.toml"
PYRIGHT_CONFIG_NAME = "pyrightconfig.json"


@dataclass(frozen=True)
class VirtualEnvironment:
    """A detected environment. `path` preserves the project-facing path (incl. symlinks)."""

    path: Path
    source: str
    python: Path | None
    version: str | None
    is_project_local: bool


@dataclass
class PythonToolingResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    environment: VirtualEnvironment | None = None
    install_candidates: list[str] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)
    install_failures: list[str] = field(default_factory=list)
    install_declined: bool = False
    python_version: str | None = None


def _is_relative_to(path: Path, root: Path) -> bool:
    """Lexical containment: a project-local symlink such as .venv may target a cache."""
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def _python_executable(environment: Path) -> Path | None:
    candidates = (
        environment / "bin" / "python",
        environment / "bin" / "python3",
        environment / "Scripts" / "python.exe",
        environment / "Scripts" / "python",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _version_from_python(executable: Path | str | None) -> str | None:
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"Python\s+(\d+\.\d+(?:\.\d+)?)", completed.stdout or "")
    return match.group(1) if completed.returncode == 0 and match else None


def _is_environment(path: Path) -> bool:
    return (
        (path / "pyvenv.cfg").is_file() or (path / "conda-meta").is_dir()
    ) and _python_executable(path) is not None


def _make_environment(path: Path, *, root: Path, source: str) -> VirtualEnvironment | None:
    if not path.is_dir() or not _is_environment(path):
        return None
    executable = _python_executable(path)
    return VirtualEnvironment(
        path=path.absolute(),
        source=source,
        python=executable.absolute() if executable else None,
        version=_version_from_python(executable),
        is_project_local=_is_relative_to(path, root),
    )


def detect_virtual_environment(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> VirtualEnvironment | None:
    """Detect project/active virtualenvs without creating, deleting, or modifying them.

    Explicit UV_PROJECT_ENVIRONMENT wins. A project-local environment wins over an
    unrelated VIRTUAL_ENV inherited from the caller's shell.
    """
    env = os.environ if environ is None else environ
    root = project_root.absolute()

    explicit_uv_env = env.get("UV_PROJECT_ENVIRONMENT")
    if explicit_uv_env:
        candidate = Path(explicit_uv_env).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        detected = _make_environment(candidate, root=root, source="UV_PROJECT_ENVIRONMENT")
        if detected:
            return detected

    # Prefer conventional project environments over a possibly stale active shell env.
    for name in (".venv", "venv", "env", "virtualenv", "ENV"):
        detected = _make_environment(root / name, root=root, source=f"каталог {name}/")
        if detected:
            return detected

    active_external: list[VirtualEnvironment] = []
    for variable, source in (
        ("VIRTUAL_ENV", "активированное VIRTUAL_ENV"),
        ("CONDA_PREFIX", "активированное Conda-окружение"),
    ):
        raw = env.get(variable)
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        detected = _make_environment(candidate, root=root, source=source)
        if detected:
            if detected.is_project_local:
                return detected
            active_external.append(detected)

    return active_external[0] if active_external else None


def _read_toml(path: Path) -> dict:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise DevEnvError(f"Не удалось прочитать TOML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DevEnvError(f"Корень TOML должен быть таблицей: {path}")
    return value


def _nested_get(value: Mapping, dotted_path: str, default=None):
    current = value
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _python_version_from_constraints(value: str) -> str | None:
    """Choose a conservative Python minor from common PEP 440 constraints."""
    match = re.search(r"(?:^|[,\s])(?:>=|==|~=|>|=)\s*(\d+)\.(\d+)", value)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.\d+)?\s*", value)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return None


def _system_python_version(root: Path | None = None) -> str | None:
    cwd = root or Path.cwd()
    for command in ("python", "python3"):
        executable = _resolve_command_executable(command, cwd=cwd)
        version = _version_from_python(executable)
        if version:
            return ".".join(version.split(".")[:2])
    return None


def _select_python_version(
    root: Path,
    *,
    existing_mise: dict | None,
    environment: VirtualEnvironment | None,
) -> str:
    tools = existing_mise.get("tools", {}) if isinstance(existing_mise, dict) else {}
    configured = tools.get("python") if isinstance(tools, dict) else None
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    if isinstance(configured, list) and configured and isinstance(configured[0], str):
        return configured[0]
    if isinstance(configured, dict) and isinstance(configured.get("version"), str):
        return configured["version"]

    version_file = root / ".python-version"
    if version_file.is_file():
        content = version_file.read_text(encoding="utf-8").strip().splitlines()
        if content and content[0].strip():
            return content[0].strip()

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            project = _read_toml(pyproject).get("project", {})
        except DevEnvError:
            project = {}
        requirement = project.get("requires-python") if isinstance(project, dict) else None
        if isinstance(requirement, str):
            selected = _python_version_from_constraints(requirement)
            if selected:
                return selected

    # Do not let an unrelated active shell environment dictate this project's Python.
    if environment and (environment.is_project_local or environment.source == "UV_PROJECT_ENVIRONMENT"):
        if environment.version:
            return ".".join(environment.version.split(".")[:2])
    return _system_python_version(root) or DEFAULT_PYTHON_VERSION


def _section_header(section: str) -> re.Pattern[str]:
    escaped = re.escape(section)
    return re.compile(rf"(?m)^\[{escaped}\][ \t]*(?:#.*)?$")


def _replace_table_value(content: str, section: str, key: str, value: str) -> tuple[str, bool]:
    """Replace one simple TOML key in a table, preserving surrounding text/comments."""
    header = _section_header(section).search(content)
    if not header:
        return content, False

    remainder = content[header.end():]
    next_header = re.search(r"(?m)^\s*\[[^\]\r\n]+\][ \t]*(?:#.*)?$", remainder)
    end = header.end() + next_header.start() if next_header else len(content)
    table_text = content[header.end():end]
    key_pattern = re.compile(
        rf"(?m)^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)([^#\r\n]*?)([ \t]*(?:#.*)?)$"
    )
    updated, count = key_pattern.subn(lambda match: f"{match.group(1)}{value}{match.group(3)}", table_text, count=1)
    if count == 0:
        return content, False
    return content[:header.end()] + updated + content[end:], True


def _insert_table_values(content: str, section: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Append missing key/value pairs to a TOML table without reserializing existing text."""
    if not values:
        return content, []

    match = _section_header(section).search(content)
    additions: list[str] = []
    if match:
        next_header = re.search(r"(?m)^\s*\[[^\]\r\n]+\]\s*(?:#.*)?$", content[match.end():])
        end = match.end() + next_header.start() if next_header else len(content)
        table_text = content[match.end():end]
        for key, line in values.items():
            plain_key = key.strip('"')
            if re.search(rf"(?m)^\s*(?:{re.escape(key)}|{re.escape(plain_key)})\s*=", table_text):
                continue
            additions.append(line)
        if not additions:
            return content, []
        updated = content[:end].rstrip("\r\n") + "\n" + "\n".join(additions) + "\n" + content[end:].lstrip("\r\n")
        return updated, additions

    prefix = content.rstrip()
    if prefix:
        prefix += "\n\n"
    return prefix + f"[{section}]\n" + "\n".join(values.values()) + "\n", list(values.values())


def _render_task(
    run: str | None = None,
    *,
    description: str,
    depends: tuple[str, ...] = (),
    interactive: bool = False,
) -> str:
    lines = [f"description = {json.dumps(description, ensure_ascii=False)}"]
    if run is not None:
        lines.append(f"run = {json.dumps(run, ensure_ascii=False)}")
    if depends:
        lines.append("depends = [" + ", ".join(json.dumps(item) for item in depends) + "]")
    if interactive:
        lines.append("interactive = true")
    return "\n".join(lines)


def _tmux_bootstrap_package() -> str | None:
    """Return the Mise bootstrap package for the current supported host."""
    system = platform.system()
    if system == "Linux":
        return "apt:tmux"
    if system == "Darwin":
        return "brew:tmux"
    return None


def _project_environment_path(root: Path, environment: VirtualEnvironment | None) -> Path | None:
    if environment and environment.is_project_local:
        return environment.path
    return None


def _relative_project_path(path: Path, root: Path) -> str:
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError as exc:
        raise DevEnvError(f"Виртуальное окружение находится вне проекта: {path}") from exc


def _requirements_install_command(root: Path, environment: VirtualEnvironment | None) -> str:
    existing_local = environment if environment and environment.is_project_local else None
    env_path = _project_environment_path(root, existing_local) or (root / ".venv")
    relative_env = _relative_project_path(env_path, root)
    if existing_local and existing_local.python:
        relative_python = _relative_project_path(existing_local.python, root)
        create_command = ""
    else:
        if os.name == "nt":
            relative_python = (Path(relative_env) / "Scripts" / "python.exe").as_posix()
        else:
            relative_python = (Path(relative_env) / "bin" / "python").as_posix()
        # Create only when no project-local environment was found. In particular,
        # don't run `uv venv --allow-existing` against a user's existing environment.
        create_command = f"uv venv {shlex.quote(relative_env)} && "

    # Project-relative paths keep mise.toml portable across clone locations.
    # Explicit --python prevents uv pip from touching the system interpreter.
    python_arg = shlex.quote(relative_python)
    requirements_arg = shlex.quote("requirements.txt")
    return f"{create_command}uv pip install --python {python_arg} -r {requirements_arg}"


def _ensure_mise_config(
    root: Path,
    *,
    environment: VirtualEnvironment | None,
    python_version_override: str | None = None,
) -> tuple[list[str], list[str], dict]:
    path = root / "mise.toml"
    if path.is_symlink():
        raise DevEnvError("mise.toml является символической ссылкой; dev-env не будет его изменять.")

    existing: dict = {}
    content = ""
    if path.exists():
        if not path.is_file():
            raise DevEnvError("mise.toml существует, но не является обычным файлом.")
        content = path.read_text(encoding="utf-8")
        existing = _read_toml(path)

    python_version = (
        python_version_override
        or _select_python_version(root, existing_mise=existing, environment=environment)
    )
    tool_values = {
        "python": f"python = {json.dumps(python_version)}",
        "uv": 'uv = "latest"',
        '"pypi:ruff"': '"pypi:ruff" = "latest"',
        "node": 'node = "24"',
        '"npm:pyright"': '"npm:pyright" = "latest"',
        '"npm:opencode-ai"': '"npm:opencode-ai" = { version = "latest", allow_builds = ["opencode-ai"] }',
    }
    parsed_tools = existing.get("tools", {})
    if not isinstance(parsed_tools, dict):
        raise DevEnvError("В mise.toml секция [tools] должна быть таблицей.")
    additions: list[str] = []
    python_version_changed = False
    if "python" in parsed_tools and python_version_override is not None:
        current_python = _configured_tool_version(existing, "python", DEFAULT_PYTHON_VERSION)
        if current_python != python_version:
            content, replaced = _replace_table_value(
                content, "tools", "python", json.dumps(python_version)
            )
            if not replaced:
                raise DevEnvError(
                    "Не удалось безопасно изменить tools.python в mise.toml; файл оставлен без изменений."
                )
            python_version_changed = True
            additions.append(f'python = {json.dumps(python_version)}')

    missing_tools = {
        key: value for key, value in tool_values.items()
        if key.strip('"') not in parsed_tools
    }
    if missing_tools:
        content, inserted = _insert_table_values(content, "tools", missing_tools)
        additions.extend(inserted)

    # tmux is a host-level dependency, not a versioned project tool. Mise's
    # bootstrap package manager installs it once per machine. Keep existing
    # bootstrap declarations intact and add only the package for this host.
    bootstrap_package = _tmux_bootstrap_package()
    bootstrap_data = existing.get("bootstrap", {})
    if not isinstance(bootstrap_data, dict):
        raise DevEnvError("В mise.toml секция [bootstrap] должна быть таблицей.")
    bootstrap_packages = bootstrap_data.get("packages", {})
    if not isinstance(bootstrap_packages, dict):
        raise DevEnvError("В mise.toml [bootstrap.packages] должна быть таблицей.")
    if bootstrap_package and bootstrap_package not in bootstrap_packages:
        content, inserted = _insert_table_values(
            content, "bootstrap.packages", {json.dumps(bootstrap_package): f'{json.dumps(bootstrap_package)} = "latest"'}
        )
        additions.extend(inserted)

    pyproject_path = root / "pyproject.toml"
    requirements_path = root / "requirements.txt"
    pyproject_data = _read_toml(pyproject_path) if pyproject_path.is_file() else {}
    project_table = pyproject_data.get("project", {})
    is_uv_project = isinstance(project_table, dict) and bool(project_table)
    is_uv_project = is_uv_project or (root / "uv.lock").is_file()

    local_environment = _project_environment_path(root, environment)
    if is_uv_project:
        lock_exists = (root / "uv.lock").is_file()
        install_command = "uv sync --locked" if lock_exists else "uv sync"
        test_command = "uv run --with pytest pytest"
    elif requirements_path.is_file():
        install_command = _requirements_install_command(root, environment)
        test_command = "python -m pytest"
    else:
        target_env = local_environment or (root / ".venv")
        relative_env = _relative_project_path(target_env, root)
        install_command = f"uv venv {shlex.quote(relative_env)} --allow-existing"
        test_command = "python -m pytest"

    tasks = {
        "install": _render_task(install_command, description="Install or synchronize Python dependencies"),
        "lint": _render_task("ruff check .", description="Run Ruff lint checks"),
        "format": _render_task("ruff format .", description="Format Python code with Ruff"),
        "typecheck": _render_task("pyright", description="Run Pyright type checking"),
        "test": _render_task(test_command, description="Run project tests"),
        "check": _render_task(description="Run lint, type checking, and tests", depends=("lint", "typecheck", "test")),
        "opencode": _render_task(
            "if [ -n \"${TMUX:-}\" ]; then\n"
            "  exec opencode\n"
            "fi\n"
            'session="opencode-${MISE_PROJECT_ROOT##*/}"\n'
            'exec tmux new-session -A -s "$session" opencode',
            description="Launch OpenCode in a project-specific tmux session",
            interactive=True,
        ),
        "serve": _render_task(
            "opencode serve --port ${OPENCODE_PORT:-4096} --hostname 127.0.0.1",
            description="Run headless OpenCode server",
        ),
    }
    existing_tasks = existing.get("tasks", {})
    if not isinstance(existing_tasks, dict):
        raise DevEnvError("В mise.toml секция [tasks] должна быть таблицей.")
    task_blocks = [
        f"[tasks.{name}]\n{body}"
        for name, body in tasks.items()
        if name not in existing_tasks
    ]

    settings = existing.get("settings", {})
    if not isinstance(settings, dict):
        raise DevEnvError("В mise.toml секция [settings] должна быть таблицей.")
    python_settings = settings.get("python", {})
    if not isinstance(python_settings, dict):
        raise DevEnvError("В mise.toml [settings.python] должна быть таблицей.")
    if "uv_venv_auto" not in python_settings:
        if python_settings:
            content, inserted = _insert_table_values(
                content, "settings.python", {"uv_venv_auto": 'uv_venv_auto = "source"'}
            )
        else:
            content, inserted = _insert_table_values(
                content, "settings", {"python.uv_venv_auto": 'python.uv_venv_auto = "source"'}
            )
        additions.extend(inserted)

    # Configure a source-only venv hook. It never creates a venv during `dev-env init`.
    env_table = existing.get("env", {})
    if not isinstance(env_table, dict):
        raise DevEnvError("В mise.toml секция [env] должна быть таблицей.")

    if is_uv_project:
        # uv's default project environment is .venv. Preserve non-standard local paths.
        if local_environment:
            try:
                relative = local_environment.relative_to(root).as_posix()
            except ValueError:
                relative = None
            if relative and relative != ".venv" and "UV_PROJECT_ENVIRONMENT" not in env_table:
                content, inserted = _insert_table_values(
                    content, "env", {"UV_PROJECT_ENVIRONMENT": f"UV_PROJECT_ENVIRONMENT = {json.dumps(relative)}"}
                )
                additions.extend(inserted)
    else:
        configured_venv = _nested_get(env_table, "_.python.venv")
        if configured_venv is None:
            target_env = local_environment or (root / ".venv")
            try:
                relative = target_env.relative_to(root).as_posix()
            except ValueError:
                relative = ".venv"
            value = f"_.python.venv = {{ path = {json.dumps(relative)}, create = false }}"
            # If a nested [env._.python] table already exists, append there rather than
            # creating the same dotted key in [env] and causing a TOML table conflict.
            nested_python = _nested_get(env_table, "_.python")
            if isinstance(nested_python, dict):
                content, inserted = _insert_table_values(content, "env._.python", {"venv": f"venv = {{ path = {json.dumps(relative)}, create = false }}"})
            else:
                content, inserted = _insert_table_values(content, "env", {"_.python.venv": value})
            additions.extend(inserted)

    for block in task_blocks:
        if content and not content.endswith("\n\n"):
            content = content.rstrip("\n") + "\n\n"
        content += block + "\n\n"

    try:
        parsed_after = tomllib.loads(content) if content.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise DevEnvError(f"Сгенерированная конфигурация mise.toml некорректна: {exc}") from exc

    created: list[str] = []
    updated: list[str] = []
    preserved: list[str] = []
    if not path.exists():
        path.write_text(content, encoding="utf-8")
        created.append("mise.toml")
    elif content != path.read_text(encoding="utf-8"):
        temporary = path.with_name("mise.toml.dev-env.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        if python_version_changed:
            updated.append(f"mise.toml (версия Python изменена на {python_version}; добавлены отсутствующие настройки)")
        else:
            updated.append("mise.toml (добавлены только отсутствующие инструменты/задачи)")
    else:
        preserved.append("mise.toml (настройки уже присутствуют)")

    return created, updated, parsed_after


def _has_ruff_config(root: Path, pyproject_data: dict | None) -> bool:
    if (root / RUFF_CONFIG_NAME).is_file() or (root / ".ruff.toml").is_file():
        return True
    tool = pyproject_data.get("tool", {}) if isinstance(pyproject_data, dict) else {}
    return isinstance(tool, dict) and isinstance(tool.get("ruff"), dict)


def _has_pyright_config(root: Path, pyproject_data: dict | None) -> bool:
    if (root / PYRIGHT_CONFIG_NAME).is_file():
        return True
    tool = pyproject_data.get("tool", {}) if isinstance(pyproject_data, dict) else {}
    return isinstance(tool, dict) and isinstance(tool.get("pyright"), dict)


def _write_if_missing(root: Path, name: str, content: str) -> str:
    path = root / name
    if path.is_symlink():
        raise DevEnvError(f"{name} является символической ссылкой; файл не будет перезаписан.")
    if path.exists():
        if not path.is_file():
            raise DevEnvError(f"{name} существует, но не является обычным файлом.")
        return "preserved"
    path.write_text(content, encoding="utf-8")
    return "created"


def _version_for_ruff(python_version: str) -> str:
    match = re.match(r"(\d+)\.(\d+)", python_version)
    return f"py{match.group(1)}{match.group(2)}" if match else "py312"


def _prepare_tool_configs(root: Path, python_version: str) -> tuple[list[str], list[str]]:
    created: list[str] = []
    preserved: list[str] = []
    pyproject_data = _read_toml(root / "pyproject.toml") if (root / "pyproject.toml").is_file() else None

    if not _has_ruff_config(root, pyproject_data):
        ruff_content = (
            f"line-length = 100\ntarget-version = \"{_version_for_ruff(python_version)}\"\n\n"
            "[lint]\nselect = [\"E4\", \"E7\", \"E9\", \"F\"]\n"
        )
        status = _write_if_missing(root, RUFF_CONFIG_NAME, ruff_content)
        (created if status == "created" else preserved).append(RUFF_CONFIG_NAME)
    else:
        preserved.append("Ruff: существующая конфигурация обнаружена")

    if not _has_pyright_config(root, pyproject_data):
        pyright_content = json.dumps(
            {
                "typeCheckingMode": "basic",
                "exclude": ["**/.venv", "**/venv", "**/__pycache__", ".git"],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        status = _write_if_missing(root, PYRIGHT_CONFIG_NAME, pyright_content)
        (created if status == "created" else preserved).append(PYRIGHT_CONFIG_NAME)
    else:
        preserved.append("Pyright: существующая конфигурация обнаружена")

    return created, preserved


def _configured_tool_version(config: dict, tool_name: str, default: str) -> str:
    tools = config.get("tools", {}) if isinstance(config, dict) else {}
    value = tools.get(tool_name, default) if isinstance(tools, dict) else default
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("version"), str):
        return value["version"]
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return default


def _mise_executable() -> str | None:
    executable = shutil.which("mise")
    if executable:
        return executable
    # The official Unix installer places mise here without requiring a PATH edit.
    local_executable = Path.home() / ".local" / "bin" / "mise"
    if local_executable.is_file() and os.access(local_executable, os.X_OK):
        return str(local_executable)
    return None


def _mise_which(command: str, *, cwd: Path) -> str | None:
    """Resolve a project-selected Mise binary without triggering installation."""
    mise = _mise_executable()
    if not mise:
        return None
    try:
        completed = subprocess.run(
            [mise, "which", command], cwd=cwd, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    executable = (completed.stdout or "").strip().splitlines()
    if not executable:
        return None
    path = Path(executable[0]).expanduser()
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def _resolve_command_executable(command: str, *, cwd: Path) -> str | None:
    """Prefer the version selected by mise, then a real system executable.

    A mise shim on PATH is not proof that a tool is installed. Resolving through
    `mise which` is read-only and avoids accidentally installing a missing tool.
    """
    if command == "mise":
        return _mise_executable()

    selected = _mise_which(command, cwd=cwd)
    if selected:
        return selected

    executable = shutil.which(command)
    if not executable:
        return None

    candidate = Path(executable)
    if candidate.is_symlink():
        try:
            target = candidate.resolve()
        except OSError:
            target = candidate
        # Mise shims are symlinks to the mise executable itself. Don't execute
        # them while detecting missing tools: a shim may trigger auto-install.
        if target.name == "mise":
            return None
    return executable


def _command_version(command: str, *, cwd: Path, args: tuple[str, ...] = ("--version",)) -> str | None:
    executable = _resolve_command_executable(command, cwd=cwd)
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, *args], cwd=cwd, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or "").strip().splitlines()[0][:180] if completed.stdout else "доступен"


def _version_matches(actual: str | None, expected: str) -> bool:
    if not actual:
        return False
    if expected in {"latest", "system", "ref:system"}:
        return True
    match = re.search(r"(\d+(?:\.\d+){0,2})", actual)
    if not match:
        return False
    wanted = re.search(r"(\d+(?:\.\d+){0,2})", expected)
    if not wanted:
        return True
    actual_parts = match.group(1).split(".")
    expected_parts = wanted.group(1).split(".")
    # Mise's short selectors such as 3.12 or node 24 specify a major/minor prefix.
    return actual_parts[: len(expected_parts)] == expected_parts


def _tool_install_candidates(root: Path, config: dict, environment: VirtualEnvironment | None) -> list[str]:
    candidates: list[str] = []
    python_expected = _configured_tool_version(config, "python", DEFAULT_PYTHON_VERSION)
    python_actual = None
    project_env = _project_environment_path(root, environment)
    if project_env:
        python_actual = _version_from_python(_python_executable(project_env))
    if not python_actual:
        python_actual = _system_python_version(root)
    python_display = f"Python {python_actual}" if python_actual else None
    if not python_actual or not _version_matches(python_display, python_expected):
        candidates.append(f"python@{python_expected}")

    for command, selector_key, default in (
        ("uv", "uv", "latest"),
        ("ruff", "pypi:ruff", "latest"),
    ):
        expected = _configured_tool_version(config, selector_key, default)
        actual = _command_version(command, cwd=root)
        if not _version_matches(actual, expected):
            candidates.append(f"{selector_key}@{expected}")

    # Node is explicitly part of the project toolchain because the managed
    # Pyright package uses Mise's npm backend. A compatible global Node version
    # is reused; otherwise Mise installs the requested project-local version.
    node_expected = _configured_tool_version(config, "node", "24")
    node_version = _command_version("node", cwd=root)
    if not _version_matches(node_version, node_expected):
        candidates.append(f"node@{node_expected}")

    pyright_expected = _configured_tool_version(config, "npm:pyright", "latest")
    pyright_version = _command_version("pyright", cwd=root)
    language_server_exists = _resolve_command_executable("pyright-langserver", cwd=root) is not None
    if not _version_matches(pyright_version, pyright_expected) or not language_server_exists:
        candidates.append(f"npm:pyright@{pyright_expected}")

    # Install OpenCode only when the project declares it. Generated configs
    # always declare it; callers that bypass _ensure_mise_config can opt out.
    config_tools = config.get("tools", {}) if isinstance(config, dict) else {}
    if isinstance(config_tools, dict) and "npm:opencode-ai" in config_tools:
        opencode_expected = _configured_tool_version(config, "npm:opencode-ai", "latest")
        opencode_version = _command_version("opencode", cwd=root)
        if not _version_matches(opencode_version, opencode_expected):
            candidates.append(f"npm:opencode-ai@{opencode_expected}")

    # Mise backends have managed-tool dependencies that are not satisfied by
    # executables found on the system PATH. In particular, `pypi:` requires a
    # Mise-managed Python even when the project already has a compatible venv;
    # `npm:` similarly needs a Mise-managed Node.js. Install those dependencies
    # first. `mise install <selector>` is idempotent, so this is safe when the
    # dependency is already installed under Mise.
    if any(selector.startswith("pypi:") for selector in candidates):
        # The Mise PyPI backend requires Mise-managed runtimes, not merely
        # matching executables found on the user's PATH. A system `python` or
        # `uv` therefore does not satisfy this dependency. `mise which` is the
        # authoritative, non-installing check for the project-selected tool.
        if not _mise_which("python", cwd=root):
            python_expected = _configured_tool_version(config, "python", DEFAULT_PYTHON_VERSION)
            if not any(selector.startswith("python@") for selector in candidates):
                candidates.insert(0, f"python@{python_expected}")

        if not _mise_which("uv", cwd=root):
            uv_expected = _configured_tool_version(config, "uv", "latest")
            if not any(selector.startswith("uv@") for selector in candidates):
                pypi_index = next(i for i, selector in enumerate(candidates) if selector.startswith("pypi:"))
                candidates.insert(pypi_index, f"uv@{uv_expected}")

    if any(selector.startswith("npm:") for selector in candidates):
        if not any(selector.startswith("node@") for selector in candidates):
            node_expected = _configured_tool_version(config, "node", "24")
            # Keep Node before the npm-backed package in installation order.
            npm_index = next(i for i, selector in enumerate(candidates) if selector.startswith("npm:"))
            candidates.insert(npm_index, f"node@{node_expected}")

    # Stable order and no duplicate selectors.
    return list(dict.fromkeys(candidates))


def _confirm_install(
    candidates: list[str],
    *,
    assume_yes: bool,
    input_fn: Callable[[str], str] = input,
) -> bool:
    if not candidates:
        return False
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    package_list = ", ".join(candidates)
    answer = input_fn(f"Будут установлены через Mise: {package_list}. Продолжить? [y/N] ").strip().lower()
    return answer in {"y", "yes", "д", "да"}


def _run_mise_install(root: Path, candidates: list[str]) -> tuple[list[str], list[str], list[str]]:
    installed: list[str] = []
    failures: list[str] = []
    warnings: list[str] = []
    mise = _mise_executable()
    if not mise:
        return installed, ["Mise не найден; установку инструментов выполнить не удалось"], warnings

    try:
        trust = subprocess.run(
            [mise, "trust", str(root / "mise.toml")], cwd=root,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return installed, [f"mise trust: {exc}"], warnings
    if trust.returncode != 0:
        detail = (trust.stdout or "").strip().splitlines()
        failures.append("mise trust: " + (detail[-1] if detail else f"код {trust.returncode}"))
        return installed, failures, warnings

    for selector in candidates:
        try:
            completed = subprocess.run(
                [mise, "install", selector], cwd=root,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900, check=False,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{selector}: превышено время ожидания")
            continue
        except OSError as exc:
            failures.append(f"{selector}: {exc}")
            continue
        if completed.returncode == 0:
            installed.append(selector)
        else:
            lines = (completed.stdout or "").strip().splitlines()
            # Mise often prints a generic final error line after the useful
            # dependency hint. Keep a bounded tail so init exposes the actual
            # cause without flooding the terminal with debug/backtrace output.
            detail = "\n    ".join(lines[-8:]) if lines else f"код {completed.returncode}"
            failures.append(f"{selector}:\n    {detail}")

    if installed:
        try:
            locked = subprocess.run(
                [mise, "lock"], cwd=root, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"mise lock: {exc}")
        else:
            if locked.returncode != 0:
                lines = (locked.stdout or "").strip().splitlines()
                warnings.append("mise lock: " + (lines[-1] if lines else f"код {locked.returncode}"))
    return installed, failures, warnings


def _confirm_mise_install(*, assume_yes: bool, input_fn: Callable[[str], str]) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input_fn(
        "Mise не найден. Скачать официальный установщик с https://mise.run "
        "и установить ~/.local/bin/mise? [y/N] "
    ).strip().lower()
    return answer in {"y", "yes", "д", "да"}


def _install_mise() -> tuple[bool, str]:
    """Install Mise with its official installer, only after an explicit confirmation."""
    if os.name == "nt":
        return False, "автоматический bootstrap сейчас поддерживает Linux/macOS; Windows: `winget install jdx.mise`"
    curl = shutil.which("curl")
    shell = shutil.which("sh")
    if not curl or not shell:
        return False, "для официального установщика требуются curl и sh"
    try:
        downloaded = subprocess.run(
            [curl, "-fsSL", "https://mise.run"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"не удалось скачать установщик Mise: {exc}"
    if downloaded.returncode != 0 or not downloaded.stdout.strip():
        detail = downloaded.stderr.strip().splitlines()
        return False, "ошибка загрузки установщика Mise: " + (detail[-1] if detail else "пустой ответ")

    install_env = os.environ.copy()
    install_env["MISE_INSTALL_PATH"] = str(Path.home() / ".local" / "bin" / "mise")
    install_env["MISE_INSTALL_HELP"] = "0"
    try:
        installed = subprocess.run(
            [shell], input=downloaded.stdout, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=300, check=False, env=install_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"ошибка запуска установщика Mise: {exc}"
    executable = _mise_executable()
    if installed.returncode != 0 or executable is None:
        lines = (installed.stdout or "").strip().splitlines()
        detail = lines[-1] if lines else f"код {installed.returncode}"
        return False, f"Mise не установился: {detail}"
    version = _command_version("mise", cwd=Path.cwd()) or "версия не определена"
    return True, f"установлен {executable} ({version})"


def _confirm_mise_bootstrap(*, assume_yes: bool, input_fn: Callable[[str], str]) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    package = _tmux_bootstrap_package() or "tmux"
    answer = input_fn(
        f"Mise установит системный пакет {package} (может запросить sudo). Продолжить? [y/N] "
    ).strip().lower()
    return answer in {"y", "yes", "д", "да"}


def _run_mise_bootstrap(root: Path, package: str) -> tuple[bool, str]:
    """Install only tmux, without applying unrelated bootstrap packages."""
    mise = _mise_executable()
    if not mise:
        return False, "Mise не найден; установить tmux через bootstrap не удалось"
    try:
        completed = subprocess.run(
            [mise, "bootstrap", "packages", "use", package], cwd=root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"mise bootstrap packages use {package}: {exc}"
    if completed.returncode == 0 and shutil.which("tmux"):
        return True, (completed.stdout or "").strip() or "tmux установлен"
    lines = (completed.stdout or "").strip().splitlines()
    detail = "\n    ".join(lines[-8:]) if lines else f"код {completed.returncode}"
    return False, f"Mise не установил {package}:\n    {detail}"


def _choose_python_version(
    root: Path,
    environment: VirtualEnvironment | None,
    *,
    interactive: bool,
    input_fn: Callable[[str], str],
) -> str:
    """Ask for a Python version only when the project has no explicit Mise choice."""
    mise_path = root / "mise.toml"
    existing = _read_toml(mise_path) if mise_path.is_file() and not mise_path.is_symlink() else {}
    # Existing Mise selection is the default, not a reason to suppress the
    # interactive choice. This lets users deliberately change a project that
    # was initialized earlier with the default Python version.
    detected = _select_python_version(root, existing_mise=existing, environment=environment)
    if not interactive:
        return detected

    version_pattern = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
    while True:
        answer = input_fn(
            f"Версия Python для проекта [{detected}] "
            "(например, 3.12, 3.13 или 3.14; Enter — оставить выбранную): "
        ).strip()
        if not answer:
            return detected
        if version_pattern.fullmatch(answer):
            return answer
        print("Укажи версию в формате major.minor или major.minor.patch, например 3.13.")


def setup_python_tooling(
    root: Path,
    *,
    install_missing: bool = True,
    assume_yes: bool = False,
    input_fn: Callable[[str], str] = input,
) -> PythonToolingResult:
    """Configure Python tooling, preserving existing files and virtual environments."""
    root = root.resolve()
    result = PythonToolingResult()
    environment = detect_virtual_environment(root)
    result.environment = environment

    python_version = _choose_python_version(
        root,
        environment,
        interactive=install_missing and not assume_yes and sys.stdin.isatty(),
        input_fn=input_fn,
    )
    mise_created, mise_updates, mise_config = _ensure_mise_config(
        root, environment=environment, python_version_override=python_version
    )
    result.created.extend(mise_created)
    result.updated.extend(mise_updates)

    python_version = _configured_tool_version(mise_config, "python", DEFAULT_PYTHON_VERSION)
    result.python_version = python_version
    config_created, config_preserved = _prepare_tool_configs(root, python_version)
    result.created.extend(config_created)
    result.preserved.extend(config_preserved)

    # Keep template exclusions (AGENTS.md, .opencode, openspec, etc.) while adding
    # Python environment/cache exclusions. Never replace the whole managed block.
    manifest = load_manifest(root)
    existing_excludes = set(manifest.get("exclude_patterns", [])) if manifest else set()
    combined_excludes = tuple(sorted(existing_excludes | set(PYTHON_EXCLUDES)))
    update_exclude(root, combined_excludes)

    if manifest is not None:
        manifest.setdefault("python_tooling", {})
        manifest["python_tooling"].update({
            "python_version": python_version,
            "environment": str(environment.path) if environment else None,
            "environment_source": environment.source if environment else None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
        manifest["exclude_patterns"] = list(combined_excludes)
        save_manifest(root, manifest)

    if not install_missing:
        candidates = _tool_install_candidates(root, mise_config, environment)
        result.install_candidates = candidates
        if candidates:
            result.warnings.append(
                "Недостающие инструменты обнаружены; автоматическая установка для этой команды отключена."
            )
        if not _mise_executable():
            result.warnings.append("Mise не найден; установка инструментов пропущена.")
        return result

    if not _mise_executable():
        if _confirm_mise_install(assume_yes=assume_yes, input_fn=input_fn):
            installed, detail = _install_mise()
            if installed:
                result.updated.append(f"Mise: {detail}")
            else:
                result.install_failures.append(detail)
                return result
        else:
            result.install_declined = True
            result.warnings.append(
                "Mise не установлен. На Linux/macOS можно установить его командой "
                "`curl -fsSL https://mise.run | sh`, затем повторить `dev-env init`."
            )
            return result

    # tmux is a host package declared under [bootstrap.packages], so it is
    # installed separately from versioned [tools]. Do not run bootstrap when
    # tmux is already available.
    if _tmux_bootstrap_package() and not shutil.which("tmux"):
        if _confirm_mise_bootstrap(assume_yes=assume_yes, input_fn=input_fn):
            bootstrapped, detail = _run_mise_bootstrap(root, _tmux_bootstrap_package())
            if bootstrapped:
                result.installed.append("tmux (Mise bootstrap)")
            else:
                result.install_failures.append(detail)
        else:
            result.install_declined = True
            result.warnings.append(
                "Установка tmux пропущена. Подтверди её при следующем `dev-env init` "
                "или выполни `mise bootstrap` вручную."
            )

    candidates = _tool_install_candidates(root, mise_config, environment)
    result.install_candidates = candidates
    if not candidates:
        return result

    if not _confirm_install(candidates, assume_yes=assume_yes, input_fn=input_fn):
        result.install_declined = True
        result.warnings.append(
            "Установка инструментов пропущена. Для автоматического подтверждения используй `dev-env init --yes`."
        )
        return result

    installed, failures, lock_warnings = _run_mise_install(root, candidates)
    result.installed.extend(installed)
    result.install_failures.extend(failures)
    result.warnings.extend(lock_warnings)
    return result


def run_tool_version(
    command: str,
    *,
    cwd: Path,
    args: tuple[str, ...] = ("--version",),
) -> tuple[bool, str]:
    detail = _command_version(command, cwd=cwd, args=args)
    return (detail is not None, detail or "команда отсутствует или не смогла вернуть версию")


def diagnose_python_tooling(root: Path):
    """Check configured Python tools, real command versions, environment and config files."""
    from .detector import Diagnostic

    checks: list[Diagnostic] = []
    mise_file = root / "mise.toml"
    mise_data: dict = {}
    mise_version = _command_version("mise", cwd=root)
    checks.append(Diagnostic(
        "Python tooling", "Mise",
        "OK" if mise_version else ("MISSING" if not _mise_executable() else "WARN"),
        mise_version or "Mise не найден или не отвечает на --version",
        required=True,
    ))

    if mise_file.is_symlink():
        checks.append(Diagnostic("Mise config", "mise.toml", "ERROR", "символическая ссылка запрещена", True))
    elif not mise_file.is_file():
        checks.append(Diagnostic("Mise config", "mise.toml", "MISSING", "не найден; выполни `dev-env init`", True))
    else:
        try:
            mise_data = _read_toml(mise_file)
        except DevEnvError as exc:
            checks.append(Diagnostic("Mise config", "mise.toml", "ERROR", str(exc), True))
        else:
            checks.append(Diagnostic("Mise config", "mise.toml", "OK", "TOML корректен", True))

    environment = detect_virtual_environment(root)
    if environment:
        info = f"{environment.path} ({environment.source})"
        if environment.version:
            info += f", Python {environment.version}"
        else:
            info += ", версия Python не определена"
        checks.append(Diagnostic("Python environment", "Виртуальное окружение", "OK", info, required=False))
    elif (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        checks.append(Diagnostic(
            "Python environment", "Виртуальное окружение", "INFO",
            "не найдено; `mise run install` создаст его при необходимости",
        ))
    else:
        checks.append(Diagnostic(
            "Python environment", "Виртуальное окружение", "INFO",
            "проект пока не содержит pyproject.toml или requirements.txt",
        ))

    # Prefer the project's existing local venv. Otherwise ask Mise for the
    # project-selected Python before falling back to a system interpreter. This
    # matters when Mise is installed but its shims are not activated in the
    # shell that invoked `dev-env doctor`.
    if environment and environment.python and environment.is_project_local:
        python_command = str(environment.python)
    else:
        python_command = _resolve_command_executable("python", cwd=root)
        if not python_command:
            python_command = _resolve_command_executable("python3", cwd=root)
    python_detail = _version_from_python(python_command) if python_command else None
    expected_python = _configured_tool_version(mise_data, "python", DEFAULT_PYTHON_VERSION)
    if python_detail:
        actual_label = f"Python {python_detail} ({python_command})"
        matches = _version_matches(actual_label, expected_python)
        checks.append(Diagnostic(
            "Python tooling", "Python", "OK" if matches else "WARN",
            actual_label + (f"; ожидается {expected_python}" if not matches else ""),
            required=True,
        ))
    else:
        checks.append(Diagnostic("Python tooling", "Python", "MISSING", "python/python3 не найдены в PATH", True))

    for label, command, selector, default in (
        ("uv", "uv", "uv", "latest"),
        ("Ruff", "ruff", "pypi:ruff", "latest"),
        ("Node.js", "node", "node", "24"),
        ("Pyright", "pyright", "npm:pyright", "latest"),
    ):
        detail = _command_version(command, cwd=root)
        expected = _configured_tool_version(mise_data, selector, default)
        if detail:
            matches = _version_matches(detail, expected)
            checks.append(Diagnostic(
                "Python tooling", label, "OK" if matches else "WARN",
                detail + (f"; ожидается {expected}" if not matches else ""), required=True,
            ))
        else:
            checks.append(Diagnostic(
                "Python tooling", label, "MISSING",
                f"команда `{command}` не найдена или не отвечает на --version", required=True,
            ))

    opencode_path = _mise_which("opencode", cwd=root)
    checks.append(Diagnostic(
        "OpenCode", "opencode",
        "OK" if opencode_path else "WARN",
        f"mise which opencode → {opencode_path}" if opencode_path else
        "не найден через Mise; выполни `mise install` (npm:opencode-ai с allow_builds)",
        required=True,
    ))

    tmux_path = shutil.which("tmux")
    checks.append(Diagnostic(
        "Terminal", "tmux", "OK" if tmux_path else "MISSING",
        f"tmux → {tmux_path}" if tmux_path else
        "не найден; выполни `dev-env init` с подтверждением установки или `mise bootstrap`",
        required=True,
    ))

    pyright_lsp = _resolve_command_executable("pyright-langserver", cwd=root)
    checks.append(Diagnostic(
        "LSP", "Pyright language server",
        "OK" if pyright_lsp else "MISSING",
        f"pyright-langserver → {pyright_lsp}" if pyright_lsp else
        "не найден в PATH; проверь npm:pyright и активацию Mise",
        required=True,
    ))

    pyproject_data: dict | None = None
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            pyproject_data = _read_toml(pyproject)
        except DevEnvError as exc:
            checks.append(Diagnostic("Python config", "pyproject.toml", "ERROR", str(exc), True))
        else:
            checks.append(Diagnostic("Python config", "pyproject.toml", "OK", "TOML корректен", False))

    ruff_path = next((root / name for name in ("ruff.toml", ".ruff.toml") if (root / name).is_file()), None)
    if ruff_path:
        try:
            _read_toml(ruff_path)
        except DevEnvError as exc:
            checks.append(Diagnostic("Ruff config", ruff_path.name, "ERROR", str(exc), True))
        else:
            checks.append(Diagnostic("Ruff config", ruff_path.name, "OK", "TOML корректен", False))
    elif _has_ruff_config(root, pyproject_data):
        checks.append(Diagnostic("Ruff config", "pyproject.toml [tool.ruff]", "OK", "секция найдена", False))
    else:
        checks.append(Diagnostic("Ruff config", "Конфигурация", "WARN", "ruff.toml и [tool.ruff] не найдены", False))

    pyright_path = root / PYRIGHT_CONFIG_NAME
    if pyright_path.is_file():
        try:
            pyright_data = json.loads(pyright_path.read_text(encoding="utf-8"))
            if not isinstance(pyright_data, dict):
                raise ValueError("корневое значение должно быть JSON-объектом")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            checks.append(Diagnostic("Pyright config", PYRIGHT_CONFIG_NAME, "ERROR", str(exc), True))
        else:
            checks.append(Diagnostic("Pyright config", PYRIGHT_CONFIG_NAME, "OK", "JSON корректен", False))
    elif _has_pyright_config(root, pyproject_data):
        checks.append(Diagnostic("Pyright config", "pyproject.toml [tool.pyright]", "OK", "секция найдена", False))
    else:
        checks.append(Diagnostic(
            "Pyright config", "Конфигурация", "WARN",
            "pyrightconfig.json и [tool.pyright] не найдены", False,
        ))

    if mise_data:
        tools = mise_data.get("tools", {})
        tasks = mise_data.get("tasks", {})
        expected_tools = set(DEFAULT_TOOLS)
        expected_tasks = {"install", "lint", "format", "typecheck", "test", "check", "opencode", "serve"}
        missing_tools = sorted(expected_tools - set(tools if isinstance(tools, dict) else {}))
        missing_tasks = sorted(expected_tasks - set(tasks if isinstance(tasks, dict) else {}))
        checks.append(Diagnostic(
            "Mise config", "Инструменты",
            "OK" if not missing_tools else "WARN",
            "все стандартные инструменты объявлены" if not missing_tools else
            "не объявлены: " + ", ".join(missing_tools), required=True,
        ))
        checks.append(Diagnostic(
            "Mise config", "Задачи",
            "OK" if not missing_tasks else "WARN",
            "все стандартные задачи объявлены" if not missing_tasks else
            "не объявлены: " + ", ".join(missing_tasks), required=True,
        ))

    return checks
