from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .errors import DevEnvError

INSTALL_URL = "https://openspec.dev/cli/install.sh"


@dataclass
class OpenSpecResult:
    status: str = "skipped"
    executable: str | None = None
    version: str | None = None
    initialized: bool = False
    updated: bool = False
    install_declined: bool = False
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output: str = ""


def find_openspec() -> str | None:
    """Find OpenSpec on PATH, including its documented standalone install path."""
    executable = shutil.which("openspec")
    if executable:
        return executable

    # The official Linux/macOS installer defaults to ~/.local/bin.
    install_dir = os.environ.get("OPENSPEC_INSTALL_DIR")
    candidate = Path(install_dir).expanduser() / "openspec" if install_dir else Path.home() / ".local" / "bin" / "openspec"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def openspec_version(executable: str | None = None, *, timeout: float = 8.0) -> str | None:
    command = executable or find_openspec()
    if not command:
        return None
    try:
        completed = subprocess.run(
            [command, "--version"], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        return None
    return output.splitlines()[0][:160]


def _confirm_install(*, assume_yes: bool, input_fn: Callable[[str], str]) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "OpenSpec CLI не найден. Для установки официальным standalone-инсталлятором "
            "повтори команду с --yes или установи OpenSpec вручную: "
            "https://openspec.dev/docs/installation"
        )
        return False
    answer = input_fn("Установить OpenSpec CLI в пользовательский каталог ~/.local/bin? [y/N] ")
    return answer.strip().lower() in {"y", "yes", "д", "да"}


def install_openspec(*, timeout: float = 90.0) -> str:
    """Run the official standalone installer after explicit user approval."""
    if os.name == "nt":
        raise DevEnvError(
            "Автоматическая установка OpenSpec через standalone installer пока поддерживается только на Linux/macOS. "
            "См. https://openspec.dev/docs/installation"
        )
    try:
        request = urllib.request.Request(
            INSTALL_URL,
            headers={"User-Agent": "dev-env-cli/0.4.3"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            script = response.read()
    except Exception as exc:
        raise DevEnvError(f"Не удалось загрузить официальный OpenSpec installer: {exc}") from exc

    if not script or b"openspec" not in script.lower():
        raise DevEnvError("Загруженный OpenSpec installer пуст или не прошёл базовую проверку содержимого")

    try:
        completed = subprocess.run(
            ["sh"], input=script, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DevEnvError(f"Не удалось выполнить OpenSpec installer: {exc}") from exc

    output = completed.stdout.decode("utf-8", errors="replace") if completed.stdout else ""
    if completed.returncode != 0:
        detail = output.strip()[-1200:] or f"код выхода {completed.returncode}"
        raise DevEnvError(f"OpenSpec installer завершился с ошибкой: {detail}")
    return output.strip()


def setup_openspec(
    project_root: Path,
    *,
    mode: str = "init",
    assume_yes: bool = False,
    skip: bool = False,
    input_fn: Callable[[str], str] = input,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> OpenSpecResult:
    """Install OpenSpec CLI if approved, then initialize/update the project integration.

    `init` uses `openspec init --tools opencode`; OpenSpec documents this as safe to
    rerun and it adds/refreshes only the selected tool integration. `update` refreshes
    generated files in an already initialized project.
    """
    if mode not in {"init", "update"}:
        raise ValueError(f"unsupported OpenSpec mode: {mode}")
    result = OpenSpecResult()
    if skip:
        result.status = "skipped"
        result.warnings.append("OpenSpec пропущен по запросу (--skip-openspec)")
        return result

    executable = find_openspec()
    if not executable:
        if not _confirm_install(assume_yes=assume_yes, input_fn=input_fn):
            result.install_declined = True
            result.status = "not-installed"
            result.warnings.append("OpenSpec CLI не установлен; интеграция проекта не запускалась")
            return result
        installer_output = install_openspec()
        if installer_output:
            result.output = installer_output
        executable = find_openspec()
        if not executable:
            raise DevEnvError(
                "Инсталлятор завершился, но `openspec` не найден. Проверь ~/.local/bin и PATH. "
                "Открой новый shell или добавь ~/.local/bin в PATH."
            )
        result.actions.append("установлен OpenSpec CLI (standalone)")

    result.executable = executable
    result.version = openspec_version(executable)

    config_path = project_root / "openspec" / "config.yaml"
    if mode == "update":
        if not config_path.is_file():
            result.status = "not-initialized"
            result.warnings.append("openspec/config.yaml не найден; выполни `dev-env init` для инициализации")
            return result
        command = [executable, "update"]
    else:
        # The explicit tool id avoids an interactive picker on servers and CI.
        command = [executable, "init", "--tools", "opencode"]

    try:
        command_env = os.environ.copy()
        if mode == "update":
            # Keep project updates deterministic and non-interactive: the user
            # manages the global CLI version separately from generated files.
            command_env["OPENSPEC_NO_UPDATE_CHECK"] = "1"
        completed = runner(
            command,
            cwd=project_root,
            env=command_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DevEnvError(f"Команда OpenSpec превысила лимит времени: {' '.join(command)}") from exc
    except OSError as exc:
        raise DevEnvError(f"Не удалось запустить OpenSpec: {exc}") from exc

    output = (completed.stdout or "").strip()
    result.output = "\n".join(part for part in (result.output, output) if part)
    if completed.returncode != 0:
        detail = output[-1600:] if output else f"код выхода {completed.returncode}"
        raise DevEnvError(f"OpenSpec завершился с ошибкой ({completed.returncode}): {detail}")

    if mode == "init":
        result.initialized = (project_root / "openspec" / "config.yaml").is_file()
        result.status = "initialized" if result.initialized else "incomplete"
        if result.initialized:
            result.actions.append("инициализирован OpenSpec для OpenCode")
        else:
            result.warnings.append(
                "Команда OpenSpec завершилась успешно, но openspec/config.yaml не найден"
            )
    else:
        result.updated = True
        result.status = "updated"
        result.actions.append("обновлены OpenSpec skills/commands")

    return result
