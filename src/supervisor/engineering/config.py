"""Configurazione del worker engineering (`config/engineering.toml`)."""
from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from supervisor.core.config import ConfigError


@dataclass(frozen=True)
class RepoEngineering:
    repo: str
    base_branch: str = "main"
    branch_prefix: str = "fix"
    max_files_changed: int = 5
    max_lines_changed: int = 200
    max_context_files: int = 8
    max_file_bytes: int = 60000
    forbidden_paths: tuple[str, ...] = ()
    install: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    check_timeout_seconds: int = 900
    python_image: str = "python:3.11-slim"
    local_excludes: tuple[str, ...] = ()
    dependency_files: tuple[str, ...] = ()
    node_image: str = ""

    def path_problem(self, path: str) -> str:
        """Motivo per cui il worker non puo' toccare `path`, o stringa vuota."""
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts or "\\" in path or path.startswith("-"):
            return f"percorso non valido: {path!r}"
        if pure.parts and pure.parts[0] == ".git":
            return f"percorso vietato: {path}"
        for pattern in self.forbidden_paths:
            if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(pure.name, pattern):
                return f"percorso vietato ({pattern}): {path}"
        return ""


@dataclass(frozen=True)
class EngineeringConfig:
    approvers: tuple[str, ...]
    approval_label: str
    lease_minutes: int
    repos: dict[str, RepoEngineering]


def load_engineering(config_dir: Path | str) -> EngineeringConfig:
    path = Path(config_dir) / "engineering.toml"
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        repos = {}
        for item in raw.get("repos", []):
            fields: dict[str, Any] = {k: tuple(v) if isinstance(v, list) else v for k, v in item.items()}
            repos[item["repo"]] = RepoEngineering(**fields)
        config = EngineeringConfig(
            approvers=tuple(raw["approvers"]), approval_label=str(raw["approval_label"]),
            lease_minutes=int(raw.get("lease_minutes", 60)), repos=repos,
        )
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not config.approvers:
        raise ConfigError(f"{path}: serve almeno un approvatore")
    return config
