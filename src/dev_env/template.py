from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from .errors import DevEnvError
from .models import Template

METADATA_NAME = ".dev-env-template.json"
DEFAULT_INCLUDE = ("AGENTS.md", "opencode.json", ".opencode", "openspec", ".gitignore")
DEFAULT_EXCLUDE = (
    "/AGENTS.md", "/opencode.json", "/.opencode/", "/openspec/",
    "/.gitignore", "/.dev-env/",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(raw: str) -> str:
    path = PurePosixPath(raw)
    if path.is_absolute() or not raw or path == PurePosixPath(".") or any(part in {"", ".", ".."} for part in path.parts):
        raise DevEnvError(f"Недопустимый путь в манифесте шаблона: {raw!r}")
    return path.as_posix()


def load_template(root: Path, revision: str | None = None) -> Template:
    root = root.resolve()
    if not root.is_dir():
        raise DevEnvError(f"Каталог шаблона не найден: {root}")

    metadata_path = root / METADATA_NAME
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DevEnvError(f"Некорректный файл {metadata_path}: {exc}") from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get("template", {}), dict):
            raise DevEnvError(f"Файл {metadata_path} должен содержать объект template")
        config = metadata.get("template", {})
        include = config.get("include", list(DEFAULT_INCLUDE))
        exclude = config.get("exclude", list(DEFAULT_EXCLUDE))
        name = str(config.get("name", root.name))
        version = str(config.get("version", "0.1.0"))
    else:
        include = list(DEFAULT_INCLUDE)
        exclude = list(DEFAULT_EXCLUDE)
        name = root.name
        version = "0.1.0"

    if not isinstance(include, list) or not all(isinstance(item, str) for item in include):
        raise DevEnvError(f"Поле template.include должно быть списком строк: {metadata_path}")
    if not isinstance(exclude, list) or not all(isinstance(item, str) for item in exclude):
        raise DevEnvError(f"Поле template.exclude должно быть списком строк: {metadata_path}")

    safe_include = tuple(_safe_relative_path(item) for item in include)
    safe_exclude: list[str] = []
    for raw in exclude:
        value = raw.strip()
        if not value or "\\" in value or any(char in value for char in "*?[]"):
            raise DevEnvError(f"Недопустимое правило template.exclude: {raw!r}")
        normalized = value.lstrip("/").rstrip("/")
        _safe_relative_path(normalized)
        safe_exclude.append("/" + normalized + ("/" if value.endswith("/") else ""))
    if len(set(safe_include)) != len(safe_include):
        raise DevEnvError("В template.include есть повторяющиеся пути")
    if len(set(safe_exclude)) != len(safe_exclude):
        raise DevEnvError("В template.exclude есть повторяющиеся правила")
    return Template(
        root=root,
        name=name,
        version=version,
        include=safe_include,
        exclude=tuple(safe_exclude),
        revision=revision,
    )


def resolve_template_source(source: str, ref: str | None = None) -> tuple[Path, str | None, tempfile.TemporaryDirectory[str] | None]:
    """Resolve a local path or clone a remote Git template into a temporary directory."""
    candidate = Path(source).expanduser()
    if candidate.exists():
        return candidate.resolve(), None, None

    if "://" not in source and not source.startswith("git@") and not source.endswith(".git"):
        raise DevEnvError(f"Шаблон не найден как локальный путь или Git URL: {source}")

    temporary = tempfile.TemporaryDirectory(prefix="dev-env-template-")
    destination = Path(temporary.name) / "template"
    command = ["git", "clone", "--depth", "1"]
    if ref:
        command.extend(["--branch", ref])
    command.extend([source, str(destination)])
    try:
        subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=destination, check=True,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()
    except FileNotFoundError as exc:
        temporary.cleanup()
        raise DevEnvError("Git не найден. Установи Git и повтори команду.") from exc
    except subprocess.CalledProcessError as exc:
        temporary.cleanup()
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise DevEnvError(f"Не удалось получить шаблон {source}: {detail}") from exc
    return destination, revision, temporary


def iter_template_files(template: Template) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for relative in template.include:
        source = template.root / relative
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_symlink() or not source.resolve().is_relative_to(template.root):
            raise DevEnvError(f"Символические ссылки или выход за пределы шаблона запрещены: {relative}")
        if source.is_file():
            files.append((relative, source))
            continue
        if source.is_dir():
            for item in sorted(source.rglob("*")):
                rel = item.relative_to(template.root).as_posix()
                if item.is_symlink() or not item.resolve().is_relative_to(template.root):
                    raise DevEnvError(f"Символические ссылки или выход за пределы шаблона запрещены: {rel}")
                if item.is_file():
                    files.append((rel, item))
    return files


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
