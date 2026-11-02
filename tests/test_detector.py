from __future__ import annotations

import json
from pathlib import Path

from dev_env.detector import (
    global_opencode_config_paths,
    opencode_lsp_servers,
    opencode_mcp_servers,
    parse_jsonc,
)


def test_parse_jsonc_supports_comments_trailing_commas_and_comment_like_strings() -> None:
    source = r'''{
      // line comment
      "url": "https://example.com/a//b",
      "text": "this is not /* a comment */",
      "nested": {
        "enabled": true,
      }, /* block comment */
    }'''

    assert parse_jsonc(source) == {
        "url": "https://example.com/a//b",
        "text": "this is not /* a comment */",
        "nested": {"enabled": True},
    }


def test_global_config_path_respects_xdg_config_home(tmp_path: Path) -> None:
    paths = global_opencode_config_paths(
        environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg")},
        home=tmp_path / "home",
        platform="linux",
    )

    assert paths == (
        tmp_path / "xdg/opencode/opencode.json",
        tmp_path / "xdg/opencode/opencode.jsonc",
    )


def test_global_config_path_supports_windows_appdata(tmp_path: Path) -> None:
    paths = global_opencode_config_paths(
        environ={"APPDATA": str(tmp_path / "roaming")},
        home=tmp_path / "home",
        platform="win32",
    )

    assert paths[0] == tmp_path / "roaming/opencode/opencode.json"


def test_mcp_parser_supports_legacy_and_current_opencode_shapes() -> None:
    legacy = {
        "mcp": {
            "context7": {"type": "remote", "url": "https://example.com/mcp"},
            "serena": {"type": "local", "command": ["serena"]},
        }
    }
    current = {
        "mcp": {
            "servers": {
                "context7": {"type": "remote", "url": "https://example.com/mcp"},
                "serena": {"type": "local", "command": ["serena"]},
            }
        }
    }

    assert set(opencode_mcp_servers(legacy)) == {"context7", "serena"}
    assert set(opencode_mcp_servers(current)) == {"context7", "serena"}


def test_lsp_parser_reads_project_pyright_config() -> None:
    config = {
        "lsp": {
            "pyright": {
                "command": ["pyright-langserver", "--stdio"],
                "disabled": False,
            }
        }
    }

    assert opencode_lsp_servers(config)["pyright"]["command"] == ["pyright-langserver", "--stdio"]
