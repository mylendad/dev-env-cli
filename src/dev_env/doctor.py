from __future__ import annotations

from pathlib import Path

from .git import END_MARKER, START_MARKER, exclude_path, tracked_paths
from .manifest import load_manifest
from .template import sha256_file


def diagnose(root: Path) -> list[tuple[str, str]]:
    checks: list[tuple[str, str]] = []
    path = exclude_path(root)
    if not path.exists():
        checks.append(("Git exclude", f"Файл отсутствует: {path}"))
    else:
        content = path.read_text(encoding="utf-8")
        checks.append(("Git exclude", "OK" if START_MARKER in content and END_MARKER in content else "блок dev-env не найден"))

    manifest = load_manifest(root)
    if manifest is None:
        checks.append(("Manifest", "не найден — выполни `dev-env init`"))
        return checks

    checks.append(("Manifest", "OK"))
    expected_patterns = manifest.get("exclude_patterns", [])
    if path.exists() and expected_patterns:
        content = path.read_text(encoding="utf-8")
        try:
            managed_block = content.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]
        except IndexError:
            checks.append(("Ignore rules", "блок dev-env повреждён или отсутствует"))
        else:
            actual = {line.strip() for line in managed_block.splitlines() if line.strip()}
            missing = sorted(set(expected_patterns) - actual)
            checks.append(("Ignore rules", "OK" if not missing else "отсутствуют: " + ", ".join(missing)))
        tracked = tracked_paths(root, tuple(expected_patterns))
        if tracked:
            checks.append(("Tracked files", "ПРОДОЛЖАЮТ отслеживаться Git: " + ", ".join(tracked)))
    resolved_root = root.resolve()
    for relative, metadata in sorted(manifest["files"].items()):
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(resolved_root):
            checks.append((relative, "КОНФЛИКТ: символическая ссылка или путь за пределами проекта"))
        elif not target.exists():
            checks.append((relative, "ОТСУТСТВУЕТ"))
        elif not target.is_file():
            checks.append((relative, "КОНФЛИКТ: не обычный файл"))
        elif sha256_file(target) == metadata.get("sha256"):
            checks.append((relative, "OK"))
        else:
            checks.append((relative, "ИЗМЕНЁН локально"))
    return checks
