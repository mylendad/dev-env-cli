from __future__ import annotations

from pathlib import Path

from .detector import Diagnostic, detect_environment
from .git import END_MARKER, START_MARKER, exclude_path, tracked_paths
from .manifest import load_manifest
from .template import sha256_file
from .python_tools import diagnose_python_tooling


def diagnose(root: Path, *, live_mcp: bool = False) -> list[Diagnostic]:
    checks: list[Diagnostic] = []
    path = exclude_path(root)

    if not path.exists():
        checks.append(Diagnostic(
            "Git", "Локальный exclude", "WARN", f"файл отсутствует: {path}", required=True,
        ))
    else:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            checks.append(Diagnostic("Git", "Локальный exclude", "ERROR", str(exc), required=True))
        else:
            valid_block = START_MARKER in content and END_MARKER in content
            checks.append(Diagnostic(
                "Git", "Локальный exclude", "OK" if valid_block else "WARN",
                "блок dev-env найден" if valid_block else "блок dev-env не найден",
                required=True,
            ))

    manifest = load_manifest(root)
    if manifest is None:
        checks.append(Diagnostic(
            "dev-env", "Manifest", "WARN", "не найден — выполни `dev-env init`", required=True,
        ))
    else:
        checks.append(Diagnostic("dev-env", "Manifest", "OK", "найден и прочитан", required=True))
        expected_patterns = manifest.get("exclude_patterns", [])
        if path.exists() and expected_patterns:
            try:
                content = path.read_text(encoding="utf-8")
                managed_block = content.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]
            except (OSError, IndexError):
                checks.append(Diagnostic(
                    "Git", "Правила exclude", "WARN", "блок dev-env повреждён или отсутствует", True,
                ))
            else:
                actual = {line.strip() for line in managed_block.splitlines() if line.strip()}
                missing = sorted(set(expected_patterns) - actual)
                checks.append(Diagnostic(
                    "Git", "Правила exclude", "OK" if not missing else "WARN",
                    "все пути исключены" if not missing else "отсутствуют: " + ", ".join(missing),
                    required=True,
                ))

        tracked = tracked_paths(root, tuple(expected_patterns))
        if tracked:
            checks.append(Diagnostic(
                "Git", "Отслеживаемые конфигурации", "WARN",
                "уже отслеживаются Git и могут попасть в push: " + ", ".join(tracked),
                required=True,
            ))
        else:
            checks.append(Diagnostic(
                "Git", "Отслеживаемые конфигурации", "OK",
                "управляемые пути не отслеживаются Git", required=True,
            ))

        resolved_root = root.resolve()
        for relative, metadata in sorted(manifest["files"].items()):
            target = root / relative
            if target.is_symlink() or not target.resolve().is_relative_to(resolved_root):
                checks.append(Diagnostic(
                    "Файлы шаблона", relative, "ERROR",
                    "символическая ссылка или путь за пределами проекта", required=True,
                ))
            elif not target.exists():
                checks.append(Diagnostic(
                    "Файлы шаблона", relative, "MISSING", "файл отсутствует", required=True,
                ))
            elif not target.is_file():
                checks.append(Diagnostic(
                    "Файлы шаблона", relative, "ERROR", "путь больше не является обычным файлом", True,
                ))
            elif sha256_file(target) == metadata.get("sha256"):
                checks.append(Diagnostic("Файлы шаблона", relative, "OK", "без локальных изменений"))
            else:
                checks.append(Diagnostic(
                    "Файлы шаблона", relative, "WARN",
                    "изменён локально; dev-env не будет перезаписывать его без проверки",
                ))

    checks.extend(detect_environment(root, live_mcp=live_mcp))
    checks.extend(diagnose_python_tooling(root))
    return checks
