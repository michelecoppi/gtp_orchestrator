"""Catalogo modelli versionato e stima dei costi.

Il prezzo di una chiamata si calcola sempre dal catalogo con la data di oggi (Europe/Rome): un
modello senza prezzo valido per la data non si puo' usare. La stima per la prenotazione e' volutamente
pessimista (token di input sovrastimati, output al massimo consentito, margine del 10%).
"""
from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from supervisor.core.clock import ROME
from supervisor.core.config import ConfigError

# ~4 caratteri per token nel testo tipico: dividendo per 3 si sovrastima di proposito.
CHARS_PER_TOKEN = 3
MESSAGE_OVERHEAD_TOKENS = 50
SAFETY_MARGIN = 1.10


@dataclass(frozen=True)
class Price:
    start: date
    end: Optional[date]
    input_usd_per_mtok: float
    output_usd_per_mtok: float


@dataclass(frozen=True)
class ModelEntry:
    key: str
    provider: str
    litellm_model: str
    enabled: bool
    access_verified: bool
    max_input_tokens: int
    max_output_tokens: int
    prices: tuple[Price, ...]
    reasoning_effort: Optional[str] = None
    # Moltiplicatore per il costo dichiarato dal provider (es. 1.055 per la commissione OpenRouter), cosi'
    # il consuntivo resta confrontabile con i prezzi del catalogo che la includono gia'.
    provider_cost_markup: float = 1.0

    def price_on(self, day: date) -> Optional[Price]:
        for price in self.prices:
            if price.start <= day and (price.end is None or day <= price.end):
                return price
        return None


@dataclass(frozen=True)
class Catalog:
    version: str
    models: dict[str, ModelEntry]

    def get(self, key: str) -> Optional[ModelEntry]:
        return self.models.get(key)


def load_catalog(config_dir: Path | str) -> Catalog:
    path = Path(config_dir) / "models.toml"
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        models = {}
        for item in raw.get("models", []):
            prices = tuple(sorted((
                Price(date.fromisoformat(str(p["from"])),
                      date.fromisoformat(str(p["until"])) if p.get("until") else None,
                      float(p["input"]), float(p["output"]))
                for p in item.get("prices", [])), key=lambda p: p.start))
            models[item["key"]] = ModelEntry(
                key=item["key"], provider=item["provider"], litellm_model=item["litellm_model"],
                enabled=bool(item.get("enabled", False)), access_verified=bool(item.get("access_verified", False)),
                max_input_tokens=int(item["max_input_tokens"]), max_output_tokens=int(item["max_output_tokens"]),
                prices=prices, reasoning_effort=item.get("reasoning_effort"),
                provider_cost_markup=float(item.get("provider_cost_markup", 1.0)),
            )
        return Catalog(version=str(raw["version"]), models=models)
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def rome_date(now: datetime) -> date:
    return now.astimezone(ROME).date()


def estimate_input_tokens(*texts: str) -> int:
    return sum(math.ceil(len(t) / CHARS_PER_TOKEN) for t in texts) + MESSAGE_OVERHEAD_TOKENS


def cost_micros(price: Price, input_tokens: int, output_tokens: int) -> int:
    """USD per milione di token -> micro-dollari: 1 token a 1 USD/Mtok costa esattamente 1 micro-dollaro."""
    return math.ceil(input_tokens * price.input_usd_per_mtok + output_tokens * price.output_usd_per_mtok)


def reservation_micros(price: Price, input_tokens: int, max_output_tokens: int) -> int:
    return math.ceil(cost_micros(price, input_tokens, max_output_tokens) * SAFETY_MARGIN)


@dataclass(frozen=True)
class Route:
    task: str
    model: str
    max_output_tokens: int
    max_items_per_run: int = 10


def load_routing(config_dir: Path | str) -> dict[str, Route]:
    path = Path(config_dir) / "routing.toml"
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        return {task: Route(task, str(cfg.get("model", "")), int(cfg["max_output_tokens"]),
                            int(cfg.get("max_items_per_run", 10)))
                for task, cfg in (raw.get("tasks") or {}).items()}
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
