from __future__ import annotations

import json
from pathlib import Path

from dev_env.detector import detect_environment


def test_detect_environment_reads_global_context7_and_project_lsp_mcp(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_home = tmp_path / "config"
    opencode_dir = config_home / "opencode"
    opencode_dir.mkdir(parents=True)
    (opencode_dir / "opencode.json").write_text(
        json.dumps({"mcp": {"context7": {"type": "remote", "url": "https://example.com"}}}),
        encoding="utf-8",
    )
    (project / "opencode.json").write_text(
        json.dumps({
            "lsp": {"pyright": {"command": ["pyright-langserver", "--stdio"]}},
            "mcp": {"serena": {"type": "local", "command": ["serena"]}},
        }),
        encoding="utf-8",
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setattr("dev_env.detector.shutil.which", lambda command: f"/mock/{command}")

    checks = detect_environment(project)
    check_map = {(check.category, check.name): check for check in checks}

    assert check_map[("MCP", "Context7")].status == "OK"
    assert check_map[("OpenCode project", str(project / "opencode.json"))].status == "OK"
    assert check_map[("LSP / MCP", "LSP pyright (project)")].status == "OK"
    assert check_map[("LSP / MCP", "MCP serena (project)")].status == "OK"


def test_detect_environment_warns_when_context7_is_missing(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "opencode.json").write_text('{"$schema": "https://opencode.ai/config.json"}\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setattr("dev_env.detector.shutil.which", lambda command: f"/mock/{command}")

    checks = detect_environment(project)
    context7 = next(check for check in checks if check.category == "MCP" and check.name == "Context7")

    assert context7.status == "WARN"
    assert context7.failed is True


def test_detect_environment_reports_invalid_jsonc(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "opencode.json").write_text('{ invalid json }', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.setattr("dev_env.detector.shutil.which", lambda command: f"/mock/{command}")

    checks = detect_environment(project)
    project_config = next(check for check in checks if check.category == "OpenCode project")

    assert project_config.status == "ERROR"
    assert project_config.failed is True
