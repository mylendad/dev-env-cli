from __future__ import annotations

import json
import subprocess
from pathlib import Path

from dev_env.git import project_root
from dev_env.installer import install
from dev_env.template import load_template


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    git("init", cwd=path)
    git("config", "user.email", "test@example.invalid", cwd=path)
    git("config", "user.name", "Test", cwd=path)
    return path


def make_template(path: Path, *, agents: str = "rules v1\n") -> Path:
    path.mkdir()
    (path / "AGENTS.md").write_text(agents, encoding="utf-8")
    (path / "opencode.json").write_text('{"$schema":"https://opencode.ai/config.json"}\n', encoding="utf-8")
    (path / ".opencode" / "skills" / "demo").mkdir(parents=True)
    (path / ".opencode" / "skills" / "demo" / "SKILL.md").write_text("demo skill\n", encoding="utf-8")
    metadata = {
        "template": {
            "name": "test-template",
            "version": "1.0.0",
            "include": ["AGENTS.md", "opencode.json", ".opencode"],
            "exclude": ["/AGENTS.md", "/opencode.json", "/.opencode/", "/.dev-env/"],
        }
    }
    (path / ".dev-env-template.json").write_text(json.dumps(metadata), encoding="utf-8")
    return path


def test_init_copies_and_adds_local_excludes(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    template = make_template(tmp_path / "template")

    result = install(project_root(repo), load_template(template))

    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "rules v1\n"
    assert (repo / ".opencode/skills/demo/SKILL.md").exists()
    assert sorted(result.copied) == [".opencode/skills/demo/SKILL.md", "AGENTS.md", "opencode.json"]
    exclude = (repo / ".git/info/exclude").read_text(encoding="utf-8")
    assert "/AGENTS.md" in exclude
    assert "/.opencode/" in exclude


def test_init_is_idempotent_and_does_not_overwrite_user_file(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    template = make_template(tmp_path / "template")
    (repo / "AGENTS.md").write_text("my own rules\n", encoding="utf-8")

    first = install(project_root(repo), load_template(template))
    second = install(project_root(repo), load_template(template))

    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "my own rules\n"
    assert "AGENTS.md" in first.skipped
    assert "AGENTS.md" in second.skipped
    assert (repo / ".git/info/exclude").read_text(encoding="utf-8").count("# >>> dev-env managed >>>") == 1


def test_update_changes_unmodified_managed_file_but_preserves_local_edits(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    template = make_template(tmp_path / "template")
    install(project_root(repo), load_template(template))

    (template / "AGENTS.md").write_text("rules v2\n", encoding="utf-8")
    (repo / "opencode.json").write_text('{"local":true}\n', encoding="utf-8")
    result = install(project_root(repo), load_template(template), update=True)

    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == "rules v2\n"
    assert (repo / "opencode.json").read_text(encoding="utf-8") == '{"local":true}\n'
    assert "AGENTS.md" in result.copied
    assert "opencode.json" in result.conflicts


def test_existing_tracked_file_is_reported_and_not_untracked_automatically(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    template = make_template(tmp_path / "template")
    (repo / "AGENTS.md").write_text("rules v1\n", encoding="utf-8")
    git("add", "AGENTS.md", cwd=repo)

    result = install(project_root(repo), load_template(template))

    assert "AGENTS.md" in result.tracked
    assert "AGENTS.md" in git("ls-files", cwd=repo).splitlines()


def test_exclude_preserves_manual_entries_and_replaces_only_managed_block(tmp_path: Path) -> None:
    from dev_env.git import update_exclude

    repo = make_repo(tmp_path / "repo")
    exclude = repo / ".git/info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("# my local rule\n*.log\n", encoding="utf-8")
    patterns = ("/AGENTS.md", "/.opencode/")

    update_exclude(repo, patterns)
    update_exclude(repo, ("/AGENTS.md", "/.opencode/", "/openspec/"))

    content = exclude.read_text(encoding="utf-8")
    assert "# my local rule" in content
    assert "*.log" in content
    assert content.count("# >>> dev-env managed >>>") == 1
    assert "/openspec/" in content


def test_destination_symlink_is_rejected(tmp_path: Path) -> None:
    import pytest

    from dev_env.errors import DevEnvError

    repo = make_repo(tmp_path / "repo")
    template = make_template(tmp_path / "template")
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".opencode").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DevEnvError, match="Символическая ссылка"):
        install(project_root(repo), load_template(template))
    assert not (outside / "skills/demo/SKILL.md").exists()
