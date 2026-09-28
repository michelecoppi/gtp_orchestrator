"""Metriche di prodotto da PostHog, in sola lettura (HogQL via Query API).

Definizioni del gioco (docs/product-analytics.md §12, §15b) e North Star della specifica (sez. 3):
- volumi: eventi e utenti distinti per evento negli ultimi 30 giorni (controllo di qualita' dei dati);
- attivazione 24h per canale: primo `bot_started` con `is_new_user='true'`, poi `daily_guess_submitted`
  entro 24 ore; raggruppato per `acquisition_channel`, avvii negli ultimi 30 giorni;
- completamento Daily: utenti-giorno con `daily_completed` / utenti-giorno con `daily_guess_submitted`;
- ritorno a 7 giorni per coorte settimanale: primo tentativo di sempre; conta solo chi ha 7 giorni maturi;
- North Star: nuovi ingressi della settimana che completano una Daily nella Mini App entro 7 giorni
  (solo settimane mature);
- referral: aperture con `referral_attached = true` e conversioni;
- attivazione 24h per campagna: come per canale, raggruppata per `campaign_id` di `bot_started` (gioco #218:
  link `src_<fonte>-<campagna>`); chiude il giro brief di Promo -> nuovi giocatori attivati.

Regole sui dati: un evento assente vuol dire nessun denominatore, non 0%; sotto la soglia minima di utenti
si mostrano i conteggi ma il tasso non si interpreta; coorti immature senza tasso. `is_new_user` in questo
progetto PostHog e' una stringa: si confronta con 'true'.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from supervisor.collectors.http import HttpClient

TRACKED_EVENTS = ("bot_started", "miniapp_opened", "daily_guess_submitted", "daily_completed", "hint_used",
                  "referral_opened", "referral_converted")


class PostHogError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProductConfig:
    host: str
    project_id: str
    environment: str = "production"
    refresh_hours: float = 20
    min_users: int = 30
    activation_window_days: int = 30
    completion_window_days: int = 7
    return_cohort_days: int = 90
    north_star_weeks: int = 8
    alpha: float = 0.05
    power: float = 0.80
    max_weeks: int = 8


def load_product(config_dir) -> ProductConfig:
    import tomllib
    from pathlib import Path

    from supervisor.core.config import ConfigError

    path = Path(config_dir) / "product.toml"
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        return ProductConfig(**raw["posthog"], **raw.get("thresholds", {}), **raw.get("experiments", {}))
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


@dataclass
class Rate:
    """Un tasso con i suoi conteggi e lo stato della lettura."""
    numerator: Optional[int]
    denominator: Optional[int]
    status: str  # ok | sotto_soglia | non_disponibile
    note: str = ""

    @property
    def value(self) -> Optional[float]:
        if self.status == "non_disponibile" or not self.denominator:
            return None
        return (self.numerator or 0) / self.denominator


def rate(numerator: Optional[int], denominator: Optional[int], min_users: int, note: str = "") -> dict[str, Any]:
    if not denominator:
        r = Rate(numerator, denominator, "non_disponibile", note or "nessun denominatore")
    elif denominator < min_users:
        r = Rate(numerator, denominator, "sotto_soglia", note or f"meno di {min_users} utenti: solo descrittivo")
    else:
        r = Rate(numerator, denominator, "ok", note)
    return {**asdict(r), "value": r.value}


class HogQL:
    def __init__(self, http: HttpClient, config: ProductConfig, api_key: str) -> None:
        self.http = http
        self.config = config
        self.api_key = api_key

    def run(self, query: str) -> list[list[Any]]:
        if not self.api_key:
            raise PostHogError("SUP_POSTHOG_PERSONAL_API_KEY non configurata")
        url = f"{self.config.host}/api/projects/{self.config.project_id}/query/"
        try:
            response = self.http.request("POST", url, headers={"Authorization": f"Bearer {self.api_key}"},
                                         json_body={"query": {"kind": "HogQLQuery", "query": query}})
        except Exception as exc:
            raise PostHogError(f"rete verso PostHog: {type(exc).__name__}") from exc
        if response.status != 200:
            detail = response.body.get("detail", "") if isinstance(response.body, dict) else ""
            raise PostHogError(f"PostHog HTTP {response.status} {str(detail)[:160]}".strip())
        results = response.body.get("results") if isinstance(response.body, dict) else None
        if not isinstance(results, list) or any(not isinstance(row, list) for row in results):
            raise PostHogError("risposta di PostHog malformata")
        return results


def _env(config: ProductConfig) -> str:
    return f"properties.environment = '{config.environment}'"


# --- query ---------------------------------------------------------------------------------------
def volume_query(config: ProductConfig) -> str:
    events = ", ".join(f"'{e}'" for e in TRACKED_EVENTS)
    return (f"SELECT event, count() AS events, uniq(distinct_id) AS users FROM events "
            f"WHERE timestamp >= now() - INTERVAL 30 DAY AND {_env(config)} AND event IN ({events}) "
            f"GROUP BY event ORDER BY events DESC")


def activation_query(config: ProductConfig, by: str = "acquisition_channel") -> str:
    days = int(config.activation_window_days)
    return (
        "SELECT s.channel, count() AS started, countIf(g.first_guess IS NOT NULL "
        "AND g.first_guess <= s.started_at + INTERVAL 24 HOUR) AS activated FROM ("
        "  SELECT distinct_id, min(timestamp) AS started_at, "
        f"argMin(properties.{by}, timestamp) AS channel, argMin(properties.is_new_user, timestamp) AS new "
        f"  FROM events WHERE event = 'bot_started' AND {_env(config)} GROUP BY distinct_id"
        f") AS s LEFT JOIN ("
        "  SELECT distinct_id, min(timestamp) AS first_guess FROM events "
        f"  WHERE event = 'daily_guess_submitted' AND {_env(config)} GROUP BY distinct_id"
        ") AS g ON s.distinct_id = g.distinct_id "
        f"WHERE s.new = 'true' AND s.started_at >= now() - INTERVAL {days} DAY"
        + (" AND s.channel IS NOT NULL AND s.channel != ''" if by != "acquisition_channel" else "")
        + " GROUP BY s.channel ORDER BY started DESC"
    )


def campaign_query(config: ProductConfig) -> str:
    return activation_query(config, by="campaign_id")


def completion_query(config: ProductConfig) -> str:
    days = int(config.completion_window_days)
    return (
        "SELECT uniqIf(tuple(distinct_id, toDate(timestamp)), event = 'daily_guess_submitted') AS attempting, "
        "uniqIf(tuple(distinct_id, toDate(timestamp)), event = 'daily_completed') AS completed, "
        "uniqIf(tuple(distinct_id, toDate(timestamp)), event = 'hint_used') AS hinted, "
        "avgIf(toFloat(properties.attempts_used), event = 'daily_completed') AS attempts "
        f"FROM events WHERE timestamp >= now() - INTERVAL {days} DAY AND {_env(config)}"
    )


def return_query(config: ProductConfig) -> str:
    days = int(config.return_cohort_days)
    return (
        "SELECT toStartOfWeek(f.first_day, 1) AS week, count() AS users, "
        "countIf(f.first_day <= today() - 7) AS mature, "
        "countIf(f.first_day <= today() - 7 AND r.returned = 1) AS returned FROM ("
        "  SELECT distinct_id, min(toDate(timestamp)) AS first_day FROM events "
        f"  WHERE event = 'daily_guess_submitted' AND {_env(config)} GROUP BY distinct_id"
        ") AS f LEFT JOIN ("
        "  SELECT e.distinct_id AS distinct_id, 1 AS returned FROM events AS e INNER JOIN ("
        "    SELECT distinct_id, min(toDate(timestamp)) AS first_day FROM events "
        f"    WHERE event = 'daily_guess_submitted' AND {_env(config)} GROUP BY distinct_id"
        "  ) AS ff ON e.distinct_id = ff.distinct_id "
        f"  WHERE e.event = 'daily_guess_submitted' AND e.properties.environment = '{config.environment}' "
        "AND toDate(e.timestamp) > ff.first_day AND toDate(e.timestamp) <= ff.first_day + 7 "
        "  GROUP BY e.distinct_id"
        ") AS r ON f.distinct_id = r.distinct_id "
        f"WHERE f.first_day >= today() - {days} GROUP BY week ORDER BY week"
    )


def north_star_query(config: ProductConfig) -> str:
    weeks = int(config.north_star_weeks)
    return (
        "SELECT toStartOfWeek(s.started_at, 1) AS week, count() AS new_users, "
        "countIf(s.started_at <= now() - INTERVAL 7 DAY) AS mature, "
        "countIf(s.started_at <= now() - INTERVAL 7 DAY AND c.first_done IS NOT NULL "
        "AND c.first_done <= s.started_at + INTERVAL 7 DAY) AS activated FROM ("
        "  SELECT distinct_id, min(timestamp) AS started_at, argMin(properties.is_new_user, timestamp) AS new "
        f"  FROM events WHERE event = 'bot_started' AND {_env(config)} GROUP BY distinct_id"
        ") AS s LEFT JOIN ("
        "  SELECT distinct_id, min(timestamp) AS first_done FROM events "
        f"  WHERE event = 'daily_completed' AND properties.surface = 'miniapp' AND {_env(config)} "
        "GROUP BY distinct_id"
        ") AS c ON s.distinct_id = c.distinct_id "
        f"WHERE s.new = 'true' AND s.started_at >= now() - INTERVAL {weeks} WEEK GROUP BY week ORDER BY week"
    )


def referral_query(config: ProductConfig) -> str:
    return (
        "SELECT countIf(event = 'referral_opened' AND properties.referral_attached = true) AS attached, "
        "countIf(event = 'referral_converted') AS converted "
        f"FROM events WHERE timestamp >= now() - INTERVAL 30 DAY AND {_env(config)}"
    )


# --- raccolta e interpretazione -------------------------------------------------------------------
@dataclass
class ProductFacts:
    volumes: dict[str, dict[str, int]] = field(default_factory=dict)
    activation_by_channel: list[dict[str, Any]] = field(default_factory=list)
    activation_by_campaign: list[dict[str, Any]] = field(default_factory=list)
    completion: dict[str, Any] = field(default_factory=dict)
    return_cohorts: list[dict[str, Any]] = field(default_factory=list)
    north_star: list[dict[str, Any]] = field(default_factory=list)
    referral: dict[str, Any] = field(default_factory=dict)
    data_quality: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def collect_product(hogql: HogQL, config: ProductConfig) -> ProductFacts:
    """Ogni blocco ha il proprio errore: una query che fallisce non nasconde le altre."""
    facts = ProductFacts()
    min_users = config.min_users

    def guarded(name: str, fn) -> None:
        try:
            fn()
        except PostHogError as exc:
            facts.errors.append(f"{name}: {exc}")

    def volumes() -> None:
        facts.volumes = {str(r[0]): {"events": _int(r[1]), "users": _int(r[2])} for r in hogql.run(volume_query(config))}

    def activation() -> None:
        facts.activation_by_channel = [
            {"channel": str(r[0] or "unknown"), **rate(_int(r[2]), _int(r[1]), min_users)}
            for r in hogql.run(activation_query(config))
        ]

    def campaigns() -> None:
        facts.activation_by_campaign = [
            {"campaign_id": str(r[0]), **rate(_int(r[2]), _int(r[1]), min_users)}
            for r in hogql.run(campaign_query(config)) if r and r[0]
        ]

    def completion() -> None:
        rows = hogql.run(completion_query(config))
        attempting, completed, hinted, attempts = (rows[0] + [None] * 4)[:4] if rows else (0, 0, 0, None)
        facts.completion = {
            "window_days": config.completion_window_days,
            "completion": rate(_int(completed), _int(attempting), min_users, "utenti-giorno"),
            "hints": rate(_int(hinted), _int(attempting), min_users, "utenti-giorno"),
            "attempts_per_completion": round(float(attempts), 2) if isinstance(attempts, (int, float)) else None,
        }

    def returns() -> None:
        facts.return_cohorts = [
            {"week": str(r[0])[:10], "users": _int(r[1]), "mature": _int(r[2]),
             **(rate(_int(r[3]), _int(r[2]), min_users) if _int(r[2]) else
                {**rate(None, 0, min_users, "coorte immatura: nessun tasso"), "status": "immatura"})}
            for r in hogql.run(return_query(config))
        ]

    def north_star() -> None:
        facts.north_star = [
            {"week": str(r[0])[:10], "new_users": _int(r[1]), "mature": _int(r[2]), "activated": _int(r[3]),
             **(rate(_int(r[3]), _int(r[2]), min_users) if _int(r[2]) else
                {**rate(None, 0, min_users, "settimana immatura"), "status": "immatura"})}
            for r in hogql.run(north_star_query(config))
        ]

    def referral() -> None:
        rows = hogql.run(referral_query(config))
        attached, converted = (rows[0] + [0, 0])[:2] if rows else (0, 0)
        facts.referral = rate(_int(converted), _int(attached), min_users, "aperture con referral_attached")

    for name, fn in (("volumi", volumes), ("attivazione", activation), ("campagne", campaigns),
                     ("completamento", completion),
                     ("ritorno", returns), ("north_star", north_star), ("referral", referral)):
        guarded(name, fn)

    if "volumi" not in " ".join(facts.errors):
        starts = facts.volumes.get("bot_started", {}).get("events", 0)
        guesses = facts.volumes.get("daily_guess_submitted", {}).get("events", 0)
        if guesses and not starts:
            facts.data_quality.append("bot_started assente mentre arrivano eventi Daily: il denominatore dei nuovi "
                                      "ingressi manca (controllare la consegna degli eventi /start)")
        if not facts.volumes:
            facts.data_quality.append("nessun evento di produzione negli ultimi 30 giorni")
    return facts
