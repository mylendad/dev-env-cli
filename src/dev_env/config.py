from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import DevEnvError

APP_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "dev-env"
CONFIG_PATH = APP_DIR / "config.json"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DevEnvError(f"Не удалось прочитать конфигурацию {CONFIG_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise DevEnvError(f"Конфигурация должна содержать JSON-объект: {CONFIG_PATH}")
    return data


def save_config(data: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CONFIG_PATH)


def configured_template() -> str | None:
    env_value = os.environ.get("DEV_ENV_TEMPLATE")
    if env_value:
        return env_value
    value = load_config().get("template", {}).get("source")
    return str(value) if value else None
