"""Configurazione: sorgenti osservate da `config/sources.toml`, segreti e interruttori dall'ambiente.

Nessun segreto nei file versionati. `Settings.__repr__` non mostra i valori sensibili, e ogni
segreto letto viene registrato presso lo scrubber prima di qualsiasi log.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

from supervisor.core.scrub import register_secrets

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_DIR = ROOT / "config"
STORES = ("firestore", "sqlite", "memory")
_SECRET_FIELDS = ("github_token", "github_write_token", "telegram_bot_token")
# Chiavi dei provider AI: le legge LiteLLM dall'ambiente, qui si registrano solo presso lo scrubber.
PROVIDER_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GEMINI_API_KEY"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GitHubRepo:
    repo: str
    expected_default_branch: str = "main"
    ci_workflow: str = "ci.yml"
    deploy_workflow: Optional[str] = None
    workflows: tuple[str, ...] = ()
    max_open_prs: int = 20

    @property
    def source(self) -> str:
        return f"github:{self.repo}"


@dataclass(frozen=True)
class PromoConfig:
    collection: str = "promo_posts"
    stale_draft_hours: float = 24.0

    @property
    def source(self) -> str:
        return f"promo:{self.collection}"


@dataclass(frozen=True)
class Sources:
    github: tuple[GitHubRepo, ...]
    promo: PromoConfig = field(default_factory=PromoConfig)


def load_sources(config_dir: Path | str = DEFAULT_CONFIG_DIR) -> Sources:
    path = Path(config_dir) / "sources.toml"
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    repos = []
    for item in raw.get("github", []):
        if str(item.get("repo", "")).count("/") != 1:
            raise ConfigError(f"voce github senza 'owner/nome': {item!r}")
        ci = item.get("ci_workflow", "ci.yml")
        deploy = item.get("deploy_workflow")
        workflows = tuple(item.get("workflows") or ())
        workflows += tuple(w for w in (ci, deploy) if w and w not in workflows)
        repos.append(GitHubRepo(
            repo=item["repo"], expected_default_branch=item.get("expected_default_branch", "main"),
            ci_workflow=ci, deploy_workflow=deploy, workflows=workflows,
            max_open_prs=int(item.get("max_open_prs", 20)),
        ))
    try:
        promo = PromoConfig(**raw.get("promo", {}))
    except TypeError as exc:
        raise ConfigError(f"sezione [promo] non valida: {exc}") from exc
    return Sources(github=tuple(repos), promo=promo)


def _flag(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, repr=False)
class Settings:
    enabled: bool = False
    ai_enabled: bool = False
    store: str = "sqlite"
    sqlite_path: str = "supervisor.sqlite3"
    firestore_project: str = ""
    game_firestore_project: str = ""
    github_token: str = ""
    # Solo nel job dell'executor (branch e draft PR); mai nel worker che esegue codice patchato.
    github_write_token: str = ""
    telegram_bot_token: str = ""
    admin_chat_id: str = ""
    config_dir: str = str(DEFAULT_CONFIG_DIR)

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> Settings:
        env = dict(os.environ if env is None else env)
        settings = cls(
            enabled=_flag(env.get("SUP_ENABLED")),
            ai_enabled=_flag(env.get("SUP_AI_ENABLED")),
            store=(env.get("SUP_STORE") or "sqlite").strip().lower(),
            sqlite_path=env.get("SUP_SQLITE_PATH") or "supervisor.sqlite3",
            firestore_project=env.get("SUP_FIRESTORE_PROJECT", ""),
            game_firestore_project=env.get("SUP_GAME_FIRESTORE_PROJECT", ""),
            github_token=env.get("SUP_GITHUB_TOKEN") or env.get("GITHUB_TOKEN", ""),
            github_write_token=env.get("SUP_GITHUB_WRITE_TOKEN", ""),
            telegram_bot_token=env.get("SUP_TELEGRAM_BOT_TOKEN", ""),
            admin_chat_id=env.get("SUP_ADMIN_CHAT_ID", ""),
            config_dir=env.get("SUP_CONFIG_DIR") or str(DEFAULT_CONFIG_DIR),
        )
        if settings.store not in STORES:
            raise ConfigError(f"SUP_STORE non valido: {settings.store!r} (ammessi: {', '.join(STORES)})")
        register_secrets(settings.github_token, settings.github_write_token, settings.telegram_bot_token,
                         *(env.get(name) for name in PROVIDER_KEY_ENV.values()))
        return settings

    def __repr__(self) -> str:
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            shown = "***" if f.name in _SECRET_FIELDS and value else repr(value)
            parts.append(f"{f.name}={shown}")
        return "Settings(" + ", ".join(parts) + ")"
