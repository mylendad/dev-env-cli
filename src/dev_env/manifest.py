from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import DevEnvError

MANIFEST_DIR = ".dev-env"
MANIFEST_FILE = "manifest.json"
MANIFEST_VERSION = 1


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_DIR / MANIFEST_FILE


def _validate_manifest_location(root: Path, path: Path) -> None:
    root = root.resolve()
    if not path.resolve().is_relative_to(root):
        raise DevEnvError(f"Manifest path выходит за пределы проекта: {path}")
    if path.parent.is_symlink():
        raise DevEnvError(f"Каталог manifest не должен быть символической ссылкой: {path.parent}")


def load_manifest(root: Path) -> dict[str, Any] | None:
    path = manifest_path(root)
    _validate_manifest_location(root, path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevEnvError(f"Не удалось прочитать manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("manifest_version") != MANIFEST_VERSION:
        raise DevEnvError(f"Неподдерживаемый или повреждённый manifest: {path}")
    if not isinstance(data.get("files"), dict):
        raise DevEnvError(f"В manifest отсутствует объект files: {path}")
    for relative in data["files"]:
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or parsed == PurePosixPath(".") or ".." in parsed.parts:
            raise DevEnvError(f"Недопустимый путь в manifest: {relative!r}")
    return data


def save_manifest(root: Path, data: dict[str, Any]) -> None:
    path = manifest_path(root)
    _validate_manifest_location(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
