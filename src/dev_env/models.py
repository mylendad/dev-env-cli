from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Template:
    root: Path
    name: str
    version: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    revision: str | None = None


@dataclass
class InstallResult:
    copied: list[str]
    unchanged: list[str]
    skipped: list[str]
    conflicts: list[str]
    missing_sources: list[str]
    tracked: list[str]

    @classmethod
    def empty(cls) -> "InstallResult":
        return cls([], [], [], [], [], [])


JsonObject = dict[str, Any]
