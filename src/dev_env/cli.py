from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import configured_template, load_config, save_config
from .doctor import diagnose
from .errors import DevEnvError
from .git import project_root
from .installer import install
from .template import load_template, resolve_template_source
from .python_tools import setup_python_tooling
from .openspec import setup_openspec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dev-env",
        description="Установка и обслуживание персональной среды разработки в Git-проекте.",
    )
    parser.add_argument("--version", action="version", version=f"dev-env {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="установить отсутствующие конфигурации")
    init_parser.add_argument("--template", help="локальный путь или Git URL шаблона")
    init_parser.add_argument("--ref", help="ветка или тег Git-шаблона")
    init_parser.add_argument("--project", type=Path, default=Path.cwd(), help="каталог внутри целевого Git-проекта")
    init_parser.add_argument("--yes", "-y", action="store_true", help="подтвердить установку недостающих инструментов, включая OpenSpec CLI")
    init_parser.add_argument("--skip-openspec", action="store_true", help="не устанавливать и не инициализировать OpenSpec")

    update_parser = subparsers.add_parser("update", help="обновить нетронутые локально управляемые файлы")
    update_parser.add_argument("--template", help="локальный путь или Git URL шаблона")
    update_parser.add_argument("--ref", help="ветка или тег Git-шаблона")
    update_parser.add_argument("--project", type=Path, default=Path.cwd(), help="каталог внутри целевого Git-проекта")
    update_parser.add_argument("--yes", "-y", action="store_true", help="подтвердить установку OpenSpec CLI, если он отсутствует")
    update_parser.add_argument("--skip-openspec", action="store_true", help="не обновлять OpenSpec")

    doctor_parser = subparsers.add_parser("doctor", help="проверить установленную конфигурацию и инструменты")
    doctor_parser.add_argument("--project", type=Path, default=Path.cwd())
    doctor_parser.add_argument(
        "--live-mcp", action="store_true",
        help="дополнительно запустить `opencode mcp list` для проверки соединений (может обращаться к сети)",
    )

    diff_parser = subparsers.add_parser("diff", help="показать различия между проектом и шаблоном")
    diff_parser.add_argument("--template", help="локальный путь или Git URL шаблона")
    diff_parser.add_argument("--ref", help="ветка или тег Git-шаблона")
    diff_parser.add_argument("--project", type=Path, default=Path.cwd())

    config_parser = subparsers.add_parser("config", help="настройки CLI")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    set_template_parser = config_sub.add_parser("set-template", help="задать шаблон по умолчанию")
    set_template_parser.add_argument("source", help="локальный путь или Git URL шаблона")
    config_sub.add_parser("show", help="показать глобальную конфигурацию CLI")
    return parser


def _resolve(root_arg: Path, source_arg: str | None, ref: str | None):
    root = project_root(root_arg.expanduser().resolve())
    source = source_arg or configured_template()
    if not source:
        raise DevEnvError(
            "Не задан источник шаблона. Выполни `dev-env config set-template <путь-или-Git-URL>` "
            "или передай `--template`."
        )
    template_path, revision, temporary = resolve_template_source(source, ref)
    try:
        template = load_template(template_path, revision)
    except Exception:
        if temporary:
            temporary.cleanup()
        raise
    return root, template, temporary


def _print_result(result, mode: str) -> None:
    action = "Установлено" if mode == "init" else "Обновлено"
    print(f"\n{action}: {len(result.copied)}")
    for item in result.copied:
        print(f"  + {item}")
    if result.unchanged:
        print(f"Без изменений: {len(result.unchanged)}")
    if result.skipped:
        print(f"Пропущено (уже существует и не управляется dev-env): {len(result.skipped)}")
        for item in result.skipped:
            print(f"  ! {item}")
    if result.conflicts:
        print(f"Конфликты (локальные изменения сохранены): {len(result.conflicts)}")
        for item in result.conflicts:
            print(f"  ! {item}")
    if result.missing_sources:
        print(f"Отсутствуют в шаблоне (локальные файлы не удалены): {len(result.missing_sources)}")
        for item in result.missing_sources:
            print(f"  - {item}")
    if result.tracked:
        print("\nВНИМАНИЕ: следующие пути уже отслеживаются Git и продолжат попадать в коммиты:")
        for item in result.tracked:
            print(f"  ! {item}")
        print("Чтобы перестать отслеживать их, сохранив локальные файлы, используй `git rm --cached -- <путь>`. ")
    else:
        print("Правила добавлены в локальный Git exclude; файлы не будут добавляться в Git, пока они не отслеживаются.")


def _run_init(args, *, update: bool) -> int:
    root, template, temporary = _resolve(args.project, args.template, args.ref)
    try:
        result = install(root, template, update=update)
        tooling = setup_python_tooling(
            root,
            install_missing=not update,
            assume_yes=getattr(args, "yes", False),
        )
        openspec = setup_openspec(
            root,
            mode="update" if update else "init",
            assume_yes=getattr(args, "yes", False),
            skip=getattr(args, "skip_openspec", False),
        )
        print(f"Проект: {root}")
        print(f"Шаблон: {template.name} ({template.version})")
        _print_result(result, "update" if update else "init")
        print("\nPython tooling:")
        if tooling.python_version:
            print(f"  = версия Python для проекта: {tooling.python_version}")
        for item in tooling.created:
            print(f"  + создано: {item}")
        for item in tooling.updated:
            print(f"  + настроено: {item}")
        for item in tooling.preserved:
            print(f"  = сохранено: {item}")
        if tooling.environment:
            env = tooling.environment
            print(f"  = найдено окружение: {env.path} ({env.source})")
            if env.version:
                print(f"    Python {env.version}")
        else:
            print("  - существующее виртуальное окружение не найдено")
        if tooling.installed:
            print("  + установлено через Mise: " + ", ".join(tooling.installed))
        elif tooling.install_declined:
            print("  ! установка недостающих инструментов отклонена")
        elif not tooling.install_candidates and not tooling.warnings and not tooling.install_failures:
            print("  = необходимые команды уже доступны; повторная установка не требуется")
        for warning in tooling.warnings:
            print(f"  ! {warning}")
        for failure in tooling.install_failures:
            print(f"  ! ошибка установки: {failure}")
        print("\nOpenSpec:")
        if openspec.version:
            print(f"  = CLI: {openspec.version} ({openspec.executable})")
        elif openspec.executable:
            print(f"  ! CLI найден, но версию получить не удалось: {openspec.executable}")
        for action in openspec.actions:
            print(f"  + {action}")
        for warning in openspec.warnings:
            print(f"  ! {warning}")
        if openspec.output:
            print(openspec.output)
        return 1 if result.conflicts or result.tracked or tooling.install_failures or openspec.status == "incomplete" else 0
    finally:
        if temporary:
            temporary.cleanup()


def _run_diff(project: Path, source_arg: str | None, ref: str | None) -> int:
    import difflib

    from .manifest import load_manifest
    from .template import iter_template_files

    root, template, temporary = _resolve(project, source_arg, ref)
    try:
        manifest = load_manifest(root)
        if manifest is None:
            raise DevEnvError("Manifest не найден. Сначала выполни `dev-env init`.")

        sources = dict(iter_template_files(template))
        changed = False
        for relative in sorted(set(manifest["files"]) | set(sources)):
            target = root / relative
            source = sources.get(relative)
            if source is None:
                print(f"Файл больше отсутствует в шаблоне: {relative}")
                changed = True
                continue
            if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                print(f"Безопасность: путь {relative} содержит символическую ссылку или выходит за пределы проекта")
                changed = True
                continue
            if not target.exists() or not target.is_file():
                print(f"Файл отсутствует в проекте: {relative}")
                changed = True
                continue
            old_lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            new_lines = source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            diff = list(difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"project/{relative}", tofile=f"template/{relative}",
            ))
            if diff:
                changed = True
                print("".join(diff), end="")
        if not changed:
            print("Различий с шаблоном нет.")
        return 0
    finally:
        if temporary:
            temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "config":
            config = load_config()
            if args.config_command == "set-template":
                config.setdefault("template", {})["source"] = args.source
                save_config(config)
                print(f"Шаблон по умолчанию сохранён в {args.source}")
                return 0
            print(json.dumps(config, ensure_ascii=False, indent=2) if config else "Конфигурация пока не задана.")
            return 0

        if args.command == "init":
            return _run_init(args, update=False)
        if args.command == "update":
            return _run_init(args, update=True)
        if args.command == "doctor":
            root = project_root(args.project.expanduser().resolve())
            checks = diagnose(root, live_mcp=args.live_mcp)
            print(f"Проверка проекта: {root}\n")
            for check in checks:
                marker = {
                    "OK": "[OK]",
                    "INFO": "[--]",
                    "WARN": "[! ]",
                    "MISSING": "[NO]",
                    "ERROR": "[ERR]",
                }.get(check.status, "[? ]")
                print(f"{marker} {check.category} / {check.name}: {check.detail}")
            failed = any(check.failed for check in checks)
            print("\nРезультат: " + ("есть обязательные проблемы" if failed else "обязательные проверки пройдены"))
            return 1 if failed else 0
        if args.command == "diff":
            return _run_diff(args.project, args.template, args.ref)
    except DevEnvError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Ошибка файловой системы: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nОперация отменена.", file=sys.stderr)
        return 130
    parser.error("Неизвестная команда")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
