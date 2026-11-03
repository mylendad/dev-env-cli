from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .errors import DevEnvError
from .git import tracked_paths, update_exclude
from .manifest import load_manifest, save_manifest
from .models import InstallResult, Template
from .template import copy_file, iter_template_files, sha256_file
from .python_tools import PYTHON_EXCLUDES

# Personal, machine-local configuration files are intentionally kept out of Git.
# Keep these defaults even when a custom template supplies its own exclude list.
PERSONAL_CONFIG_EXCLUDES = (
    "/AGENTS.md",
    "/opencode.json",
    "/opencode.jsonc",
    "/.opencode/",
    "/openspec/",
    "/.gitignore",
    "/.dev-env/",
)


def _new_manifest(template: Template) -> dict:
    return {
        "manifest_version": 1,
        "template": {
            "name": template.name,
            "version": template.version,
            "revision": template.revision,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "files": {},
    }


def install(root: Path, template: Template, *, update: bool = False) -> InstallResult:
    result = InstallResult.empty()
    existing_manifest = load_manifest(root)
    if update and existing_manifest is None:
        raise DevEnvError("Manifest не найден. Сначала выполни `dev-env init`.")

    manifest = existing_manifest or _new_manifest(template)
    managed_files: dict = manifest["files"]
    template_files = iter_template_files(template)
    source_names = {relative for relative, _ in template_files}

    root = root.resolve()
    for relative, source in template_files:
        target = root / relative
        ancestor = root
        parts = Path(relative).parts
        for part in parts[:-1]:
            ancestor = ancestor / part
            if ancestor.is_symlink():
                raise DevEnvError(f"Символическая ссылка в пути назначения запрещена: {relative}")
        if target.is_symlink():
            raise DevEnvError(f"Файл назначения является символической ссылкой: {relative}")
        if not target.resolve().is_relative_to(root):
            raise DevEnvError(f"Путь назначения выходит за пределы проекта: {relative}")
        source_hash = sha256_file(source)
        previous = managed_files.get(relative)

        if not target.exists():
            copy_file(source, target)
            managed_files[relative] = {"sha256": source_hash}
            result.copied.append(relative)
            continue

        if target.is_symlink() or not target.is_file():
            result.conflicts.append(relative)
            continue

        current_hash = sha256_file(target)
        if current_hash == source_hash:
            result.unchanged.append(relative)
            if previous is not None:
                managed_files[relative] = {"sha256": source_hash}
            continue

        if previous is None:
            result.skipped.append(relative)
            continue

        if not update:
            result.skipped.append(relative)
            continue

        previous_hash = previous.get("sha256") if isinstance(previous, dict) else None
        if current_hash != previous_hash:
            result.conflicts.append(relative)
            continue

        copy_file(source, target)
        managed_files[relative] = {"sha256": source_hash}
        result.copied.append(relative)

    # A file removed from the source is never deleted from a user's project.
    for relative in managed_files:
        if relative not in source_names and (root / relative).exists():
            result.missing_sources.append(relative)

    combined_excludes = tuple(
        sorted(set(template.exclude) | set(PYTHON_EXCLUDES) | set(PERSONAL_CONFIG_EXCLUDES))
    )
    tracked_patterns = tuple("/" + item.lstrip("/") for item in combined_excludes)
    result.tracked = tracked_paths(root, tracked_patterns)
    exclude_file, _ = update_exclude(root, combined_excludes)

    manifest["template"] = {
        "name": template.name,
        "version": template.version,
        "revision": template.revision,
    }
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["exclude_file"] = str(exclude_file)
    manifest["exclude_patterns"] = list(combined_excludes)
    save_manifest(root, manifest)
    return result
