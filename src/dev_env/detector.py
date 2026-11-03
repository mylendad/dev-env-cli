from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Diagnostic:
    category: str
    name: str
    status: str
    detail: str
    required: bool = False

    @property
    def failed(self) -> bool:
        return self.required and self.status in {"MISSING", "WARN", "ERROR"}


@dataclass(frozen=True)
class ConfigFile:
    scope: str
    path: Path
    data: dict[str, Any] | None
    error: str | None = None


def _strip_jsonc_comments(source: str) -> str:
    """Remove JSONC comments while preserving quoted strings and line breaks."""
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False

    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if line_comment:
            if char in "\r\n":
                line_comment = False
                result.append(char)
            else:
                result.append(" ")
            index += 1
            continue

        if block_comment:
            if char == "*" and next_char == "/":
                result.extend((" ", " "))
                index += 2
                block_comment = False
            else:
                result.append(char if char in "\r\n" else " ")
                index += 1
            continue

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif char == "/" and next_char == "/":
            result.extend((" ", " "))
            index += 2
            line_comment = True
        elif char == "/" and next_char == "*":
            result.extend((" ", " "))
            index += 2
            block_comment = True
        else:
            result.append(char)
            index += 1

    if block_comment:
        raise ValueError("незакрытый комментарий /* */")
    return "".join(result)


def _remove_trailing_commas(source: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False

    while index < len(source):
        char = source[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char == ",":
            lookahead = index + 1
            while lookahead < len(source) and source[lookahead].isspace():
                lookahead += 1
            if lookahead < len(source) and source[lookahead] in "}]":
                index += 1
                continue

        result.append(char)
        index += 1

    return "".join(result)


def parse_jsonc(source: str) -> dict[str, Any]:
    cleaned = _remove_trailing_commas(_strip_jsonc_comments(source.lstrip("\ufeff")))
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("корневое значение конфигурации должно быть JSON-объектом")
    return value


def global_opencode_config_paths(
    *, environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> tuple[Path, ...]:
    """Return standard user-level OpenCode config paths for the current OS."""
    env = os.environ if environ is None else environ
    home_path = (home or Path.home()).expanduser()
    system = platform or sys.platform

    if system.startswith("win"):
        base = Path(env["APPDATA"]) if env.get("APPDATA") else home_path / "AppData" / "Roaming"
    else:
        base = Path(env.get("XDG_CONFIG_HOME", home_path / ".config")).expanduser()

    config_dir = base / "opencode"
    return (config_dir / "opencode.json", config_dir / "opencode.jsonc")


def _read_config(scope: str, path: Path) -> ConfigFile:
    if not path.is_file():
        return ConfigFile(scope=scope, path=path, data=None)
    try:
        data = parse_jsonc(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return ConfigFile(scope=scope, path=path, data=None, error=str(exc))
    return ConfigFile(scope=scope, path=path, data=data)


def load_opencode_configs(
    *, environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> list[ConfigFile]:
    env = os.environ if environ is None else environ
    configs: list[ConfigFile] = []

    for path in global_opencode_config_paths(environ=env, home=home, platform=platform):
        if path.exists():
            configs.append(_read_config("global", path))

    custom_path = env.get("OPENCODE_CONFIG")
    if custom_path:
        path = Path(custom_path).expanduser()
        if path.exists() and all(item.path != path for item in configs):
            configs.append(_read_config("custom", path))

    return configs


def opencode_mcp_servers(config: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Read MCP entries from OpenCode's legacy and current config shapes."""
    if not isinstance(config, Mapping):
        return {}
    mcp = config.get("mcp")
    if not isinstance(mcp, Mapping):
        return {}
    servers = mcp.get("servers")
    source = servers if isinstance(servers, Mapping) else mcp
    return {
        str(name): dict(item)
        for name, item in source.items()
        if name != "servers" and isinstance(item, Mapping)
    }


def opencode_lsp_servers(config: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(config, Mapping) or not isinstance(config.get("lsp"), Mapping):
        return {}
    return {
        str(name): dict(item)
        for name, item in config["lsp"].items()
        if isinstance(item, Mapping)
    }


def _is_enabled(entry: Mapping[str, Any]) -> bool:
    return entry.get("disabled") is not True and entry.get("enabled") is not False


def _command_executable(command: Any) -> str | None:
    if isinstance(command, str):
        return command.strip().split(maxsplit=1)[0] if command.strip() else None
    if isinstance(command, list) and command and isinstance(command[0], str):
        return command[0]
    return None


def _find_executable(command: str) -> str | None:
    expanded = os.path.expanduser(command)
    candidate = Path(expanded)
    if candidate.is_absolute() or os.sep in expanded:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None
    return shutil.which(command)


def _add_command_check(
    checks: list[Diagnostic],
    *,
    label: str,
    command: str,
    required: bool,
    category: str = "Команды",
) -> None:
    executable = _find_executable(command)
    if executable:
        checks.append(Diagnostic(category, label, "OK", f"{command} → {executable}", required))
    else:
        status = "MISSING" if required else "INFO"
        detail = f"команда `{command}` не найдена в PATH"
        if not required:
            detail += " (необязательная на текущем этапе)"
        checks.append(Diagnostic(category, label, status, detail, required))


def _check_configured_servers(
    checks: list[Diagnostic], *, scope: str, config: Mapping[str, Any]
) -> None:
    referenced: dict[str, list[str]] = {}

    for name, server in opencode_mcp_servers(config).items():
        if not _is_enabled(server) or server.get("type", "local") != "local":
            continue
        executable = _command_executable(server.get("command"))
        if executable:
            referenced.setdefault(executable, []).append(f"MCP {name} ({scope})")
        else:
            checks.append(Diagnostic(
                "MCP", f"{name} ({scope})", "WARN",
                "локальный MCP не содержит корректный `command`", required=True,
            ))

    for name, server in opencode_lsp_servers(config).items():
        if server.get("disabled") is True:
            continue
        executable = _command_executable(server.get("command"))
        if executable:
            referenced.setdefault(executable, []).append(f"LSP {name} ({scope})")
        else:
            checks.append(Diagnostic(
                "LSP", f"{name} ({scope})", "WARN",
                "LSP не содержит корректный `command`", required=True,
            ))

    for executable, purposes in sorted(referenced.items()):
        _add_command_check(
            checks,
            label=", ".join(purposes),
            command=executable,
            required=True,
            category="LSP / MCP",
        )


def _global_mcp_check(checks: list[Diagnostic], configs: list[ConfigFile]) -> None:
    global_configs = [item for item in configs if item.scope == "global"]
    custom_configs = [item for item in configs if item.scope == "custom"]
    valid_global = [item for item in global_configs if item.data is not None]

    if not global_configs:
        expected = global_opencode_config_paths()[0]
        checks.append(Diagnostic(
            "OpenCode global", "Глобальный конфиг", "WARN",
            f"не найден; ожидаемый путь: {expected}", required=True,
        ))
    else:
        for item in global_configs:
            if item.error:
                checks.append(Diagnostic(
                    "OpenCode global", str(item.path), "ERROR",
                    f"не удалось разобрать JSON/JSONC: {item.error}", required=True,
                ))
            elif item.data is not None:
                checks.append(Diagnostic(
                    "OpenCode global", str(item.path), "OK", "конфигурация прочитана", required=True,
                ))

    global_servers: dict[str, dict[str, Any]] = {}
    for item in valid_global:
        global_servers.update(opencode_mcp_servers(item.data))
    custom_servers: dict[str, dict[str, Any]] = {}
    for item in custom_configs:
        if item.data is not None:
            custom_servers.update(opencode_mcp_servers(item.data))

    context7 = global_servers.get("context7")
    if context7 and _is_enabled(context7):
        checks.append(Diagnostic(
            "MCP", "Context7", "OK", "настроен в глобальном OpenCode config", required=True,
        ))
    elif "context7" in custom_servers and _is_enabled(custom_servers["context7"]):
        checks.append(Diagnostic(
            "MCP", "Context7", "WARN",
            "найден в OPENCODE_CONFIG, но не в стандартном глобальном конфиге", required=True,
        ))
    else:
        reason = "не найден в глобальном конфиге"
        if context7 is not None and not _is_enabled(context7):
            reason = "присутствует в глобальном конфиге, но отключён"
        checks.append(Diagnostic("MCP", "Context7", "WARN", reason, required=True))

    for name, server in sorted(global_servers.items()):
        if name != "context7":
            state = "отключён" if not _is_enabled(server) else "настроен"
            checks.append(Diagnostic("MCP global", name, "INFO", state))


def run_live_mcp_check(project_root: Path, timeout: float = 15.0) -> Diagnostic:
    """Ask OpenCode for MCP connection states. This may start servers/network calls."""
    executable = shutil.which("opencode")
    if not executable:
        return Diagnostic("MCP live", "opencode mcp list", "MISSING", "OpenCode не найден", True)

    try:
        completed = subprocess.run(
            [executable, "mcp", "list"], cwd=project_root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return Diagnostic(
            "MCP live", "opencode mcp list", "WARN",
            f"проверка превысила лимит {timeout:g} секунд", False,
        )
    except OSError as exc:
        return Diagnostic("MCP live", "opencode mcp list", "WARN", str(exc), False)

    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        detail = output.splitlines()[-1] if output else f"код выхода {completed.returncode}"
        return Diagnostic("MCP live", "opencode mcp list", "WARN", detail, False)

    lowered = output.lower()
    if "needs authentication" in lowered or "authentication required" in lowered:
        return Diagnostic("MCP live", "Подключения", "WARN", "как минимум одному MCP нужна авторизация", False)
    if any(marker in lowered for marker in ("failed", "disconnected", "connection refused", "error:")):
        return Diagnostic("MCP live", "Подключения", "WARN", "OpenCode сообщил об ошибке одного или нескольких MCP", False)
    if "connected" in lowered:
        return Diagnostic("MCP live", "Подключения", "OK", "OpenCode сообщает о подключённых MCP", False)
    if output:
        return Diagnostic("MCP live", "Подключения", "INFO", output.splitlines()[0][:180], False)
    return Diagnostic("MCP live", "Подключения", "INFO", "команда завершилась без вывода", False)


def detect_environment(project_root: Path, *, live_mcp: bool = False) -> list[Diagnostic]:
    """Detect common tools and inspect project/global OpenCode configuration."""
    checks: list[Diagnostic] = []

    common_tools = (
        ("OpenCode", "opencode", True),
        ("pytest", "pytest", False),
    )
    for label, command, required in common_tools:
        _add_command_check(checks, label=label, command=command, required=required)

    project_paths = (project_root / "opencode.json", project_root / "opencode.jsonc")
    project_path = next((path for path in project_paths if path.is_file()), None)
    if project_path is None:
        checks.append(Diagnostic(
            "OpenCode project", "Проектный конфиг", "WARN",
            "opencode.json / opencode.jsonc не найден", required=True,
        ))
    else:
        project_config = _read_config("project", project_path)
        if project_config.error:
            checks.append(Diagnostic(
                "OpenCode project", str(project_path), "ERROR",
                f"не удалось разобрать JSON/JSONC: {project_config.error}", required=True,
            ))
        elif project_config.data is not None:
            checks.append(Diagnostic(
                "OpenCode project", str(project_path), "OK", "конфигурация прочитана", required=True,
            ))
            _check_configured_servers(checks, scope="project", config=project_config.data)

    configs = load_opencode_configs()
    _global_mcp_check(checks, configs)
    for item in configs:
        if item.data is not None:
            _check_configured_servers(checks, scope=item.scope, config=item.data)

    # OpenSpec is a first-class part of the personal development environment.
    # Its standalone installer defaults to ~/.local/bin, which may not be in PATH
    # in non-login shells (e.g. systemd/SSH automation).
    from .openspec import find_openspec, openspec_version

    openspec_executable = find_openspec()
    if openspec_executable:
        version = openspec_version(openspec_executable)
        checks.append(Diagnostic(
            "OpenSpec", "CLI", "OK" if version else "WARN",
            f"{version} → {openspec_executable}" if version else
            f"команда найдена, но `openspec --version` не сработала: {openspec_executable}",
            required=True,
        ))
    else:
        checks.append(Diagnostic(
            "OpenSpec", "CLI", "MISSING",
            "не найден; установи через `dev-env init` с подтверждением или по официальной инструкции",
            required=True,
        ))

    openspec_config = project_root / "openspec" / "config.yaml"
    if openspec_config.is_file():
        checks.append(Diagnostic(
            "OpenSpec", "Project config", "OK",
            "openspec/config.yaml найден", required=True,
        ))
        skills = sorted((project_root / ".opencode" / "skills").glob("openspec-*/SKILL.md"))
        commands = sorted((project_root / ".opencode" / "commands").glob("opsx-*.md"))
        checks.append(Diagnostic(
            "OpenSpec", "OpenCode skills", "OK" if skills else "WARN",
            f"найдено {len(skills)} OpenSpec skills в .opencode/skills/" if skills else
            "OpenSpec skills не найдены; запусти `openspec init --tools opencode`",
            required=True,
        ))
        checks.append(Diagnostic(
            "OpenSpec", "OpenCode commands", "OK" if commands else "WARN",
            f"найдено {len(commands)} OPSX-команд в .opencode/commands/" if commands else
            "OPSX-команды не найдены; проверь delivery profile и запусти `openspec init --tools opencode`",
            required=True,
        ))
    else:
        checks.append(Diagnostic(
            "OpenSpec", "Project config", "MISSING",
            "openspec/config.yaml не найден; выполни `dev-env init`",
            required=True,
        ))

    if live_mcp:
        checks.append(run_live_mcp_check(project_root))

    unique: list[Diagnostic] = []
    seen: set[tuple[str, str, str]] = set()
    for check in checks:
        key = (check.category, check.name, check.detail)
        if key not in seen:
            seen.add(key)
            unique.append(check)
    return unique
