"""Policy di autonomia versionata (`policies/autonomy.toml`), applicata dal codice.

Valori ammessi per ogni azione:
- `auto`   il supervisore procede da solo;
- `budget` procede solo entro un budget approvato (config/budget.toml: approved = true);
- `human`  serve un'approvazione di Michele, riferita a contenuto e versione precisi;
- `deny`   vietato in questa fase.
Un'azione non elencata e' negata: la policy e' un elenco di permessi, non di divieti.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from supervisor.core.config import ROOT, ConfigError

DEFAULT_POLICY_DIR = ROOT / "policies"
DECISIONS = ("auto", "budget", "human", "deny")


@dataclass(frozen=True)
class Policy:
    version: int
    level: str
    actions: dict[str, str]

    def decide(self, action: str) -> str:
        return self.actions.get(action, "deny")


def load_policy(policy_dir: Path | str = DEFAULT_POLICY_DIR) -> Policy:
    path = Path(policy_dir) / "autonomy.toml"
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    actions = {str(k): str(v) for k, v in (raw.get("actions") or {}).items()}
    invalid = {k: v for k, v in actions.items() if v not in DECISIONS}
    if invalid:
        raise ConfigError(f"{path}: valori non ammessi {invalid} (ammessi: {', '.join(DECISIONS)})")
    return Policy(version=int(raw.get("version", 0)), level=str(raw.get("level", "")), actions=actions)
