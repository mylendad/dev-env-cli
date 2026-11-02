from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import DevEnvError

START_MARKER = "# >>> dev-env managed >>>"
END_MARKER = "# <<< dev-env managed <<<"


def run_git(args: list[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise DevEnvError("Git не найден. Установи Git и повтори команду.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise DevEnvError(f"Команда git {' '.join(args)} завершилась ошибкой: {detail}") from exc
    return completed.stdout.strip()


def project_root(start: Path) -> Path:
    output = run_git(["rev-parse", "--show-toplevel"], start)
    return Path(output).resolve()


def exclude_path(root: Path) -> Path:
    raw = run_git(["rev-parse", "--git-path", "info/exclude"], root)
    path = Path(raw)
    return path if path.is_absolute() else (root / path).resolve()


def tracked_paths(root: Path, patterns: tuple[str, ...]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        # -- is important: template paths are user-controlled values.
        output = run_git(["ls-files", "--", pattern.lstrip("/")], root)
        if output:
            found.update(line for line in output.splitlines() if line)
    return sorted(found)


def update_exclude(root: Path, patterns: tuple[str, ...]) -> tuple[Path, bool]:
    path = exclude_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""

    if START_MARKER in original and END_MARKER not in original:
        raise DevEnvError(f"В {path} найден начальный маркер dev-env без конечного. Исправь блок вручную.")
    if END_MARKER in original and START_MARKER not in original:
        raise DevEnvError(f"В {path} найден конечный маркер dev-env без начального. Исправь блок вручную.")

    block_lines = [START_MARKER, *sorted(set(patterns)), END_MARKER]
    block = "\n".join(block_lines)

    if START_MARKER in original:
        before, remainder = original.split(START_MARKER, 1)
        _, after = remainder.split(END_MARKER, 1)
        updated = before.rstrip("\n")
        if updated:
            updated += "\n\n"
        updated += block
        if after.strip():
            updated += "\n" + after.lstrip("\n")
        else:
            updated += "\n"
    else:
        updated = original
        if updated and not updated.endswith("\n"):
            updated += "\n"
        if updated.strip():
            updated += "\n"
        updated += block + "\n"

    changed = updated != original
    if changed:
        temporary = path.with_name(path.name + ".dev-env.tmp")
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(path)
    return path, changed
