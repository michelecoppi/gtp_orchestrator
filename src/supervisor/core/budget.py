"""Budget AI applicato davvero: prenotazione atomica prima di ogni chiamata a pagamento.

Specifica, sez. 9. Un file di configurazione da solo non garantisce un tetto: ogni chiamata passa da
`BudgetLedger.reserve()`, che in una transazione verifica

    speso + prenotato + nuova prenotazione <= limite (giornaliero e mensile)

e registra la prenotazione con un id univoco. Dopo la risposta `settle()` registra il costo
effettivo e libera la differenza; `release()` si usa solo quando e' certo che la richiesta non e'
stata addebitata (rifiutata dal provider). Se la risposta si perde la prenotazione resta: non si
assume costo zero finche' Michele non riconcilia (`supervisor budget reconcile`).

Gli importi sono interi in micro-dollari (1 USD = 1_000_000): niente errori di arrotondamento
accumulati sommando float. Giorno e mese seguono Europe/Rome.
"""
from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from supervisor.core.clock import ROME, iso
from supervisor.core.config import ConfigError
from supervisor.state.store import BUDGET, USAGE, StateStore

MICROS = 1_000_000


def usd_to_micros(usd: float) -> int:
    return int(math.ceil(round(usd * MICROS, 6)))


def micros_to_usd(micros: int) -> float:
    return micros / MICROS


class BudgetExceeded(RuntimeError):
    """La chiamata non rientra nel budget (o il budget non e' approvato): non si invia nulla."""


@dataclass(frozen=True)
class BudgetLimits:
    daily_soft: int
    daily_hard: int
    monthly_soft: int
    monthly_hard: int
    approved: bool = False
    approved_by: str = ""
    approved_on: str = ""


@dataclass(frozen=True)
class TaskLimits:
    max_llm_calls_per_task: int = 8
    max_paid_tool_calls_per_task: int = 3
    engineering_tasks_per_repo: int = 1
    max_fix_attempts: int = 2


@dataclass(frozen=True)
class BudgetConfig:
    limits: BudgetLimits
    tasks: TaskLimits


def load_budget(config_dir: Path | str) -> BudgetConfig:
    path = Path(config_dir) / "budget.toml"
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if raw.get("currency", "USD") != "USD":
        raise ConfigError("budget.toml: solo USD e' supportato")
    try:
        limits = BudgetLimits(
            daily_soft=usd_to_micros(raw["daily_soft_limit"]), daily_hard=usd_to_micros(raw["daily_hard_limit"]),
            monthly_soft=usd_to_micros(raw["monthly_soft_limit"]),
            monthly_hard=usd_to_micros(raw["monthly_hard_limit"]),
            approved=bool(raw.get("approved", False)), approved_by=raw.get("approved_by", ""),
            approved_on=str(raw.get("approved_on", "")),
        )
        tasks = TaskLimits(**{k: int(v) for k, v in (raw.get("limits") or {}).items() if k in TaskLimits.__dataclass_fields__})
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"budget.toml non valido: {exc}") from exc
    if not (0 < limits.daily_soft <= limits.daily_hard <= limits.monthly_hard and limits.monthly_soft <= limits.monthly_hard):
        raise ConfigError("budget.toml: limiti incoerenti (soft <= hard, giornaliero <= mensile)")
    return BudgetConfig(limits, tasks)


def day_key(now: datetime) -> str:
    return now.astimezone(ROME).date().isoformat()


def month_key(now: datetime) -> str:
    return now.astimezone(ROME).strftime("%Y-%m")


def _empty_month(month: str) -> dict:
    return {"month": month, "reserved": 0, "actual": 0, "days": {}}


def _day(month_doc: dict, day: str) -> dict:
    return dict(month_doc["days"].get(day) or {"reserved": 0, "actual": 0})


@dataclass(frozen=True)
class Reservation:
    call_id: str
    amount: int
    day: str
    month: str
    warning: str = ""
    existing: bool = False
    state: str = "reserved"


class BudgetLedger:
    def __init__(self, store: StateStore, limits: BudgetLimits) -> None:
        self.store = store
        self.limits = limits

    # --- controllo senza effetti (dry-run) -------------------------------------------------
    def check(self, amount: int, now: datetime) -> Optional[str]:
        """Motivo del blocco, o None se una prenotazione di `amount` passerebbe adesso."""
        if not self.limits.approved:
            return "budget non approvato (config/budget.toml: approved = false)"
        month = self.store.get_doc(BUDGET, month_key(now)) or _empty_month(month_key(now))
        return _block_reason(month, day_key(now), amount, self.limits)

    # --- prenotazione ----------------------------------------------------------------------
    def reserve(self, call_id: str, *, task_id: str, task: str, provider: str, model: str,
                pricing_version: str, amount: int, now: datetime) -> Reservation:
        if not self.limits.approved:
            raise BudgetExceeded("budget non approvato (config/budget.toml: approved = false)")
        if amount <= 0:
            raise BudgetExceeded("importo stimato non valido")
        month, day = month_key(now), day_key(now)
        refs = [(BUDGET, month), (USAGE, call_id)]
        limits = self.limits

        def _reserve(docs):
            usage = docs[(USAGE, call_id)]
            if usage is not None:  # stessa chiamata gia' prenotata: nessuna doppia prenotazione
                return {}, Reservation(call_id, usage["reserved_micros"], usage["day"], usage["month"],
                                       existing=True, state=usage["state"])
            month_doc = docs[(BUDGET, month)] or _empty_month(month)
            reason = _block_reason(month_doc, day, amount, limits)
            if reason:
                raise BudgetExceeded(reason)
            month_doc = _add(month_doc, day, reserved=amount)
            usage = {
                "call_id": call_id, "task_id": task_id, "task": task, "provider": provider, "model": model,
                "pricing_version": pricing_version, "reserved_micros": amount, "actual_micros": None,
                "state": "reserved", "day": day, "month": month, "created_at": iso(now),
            }
            warning = _soft_warning(month_doc, day, limits)
            return {(BUDGET, month): month_doc, (USAGE, call_id): usage}, Reservation(call_id, amount, day, month, warning)

        return self.store.transact(refs, _reserve)

    def settle(self, call_id: str, actual: int, now: datetime, input_tokens: int = 0, output_tokens: int = 0,
               note: str = "") -> dict:
        return self._close(call_id, "settled", actual, now, {"input_tokens": input_tokens,
                                                              "output_tokens": output_tokens, "note": note})

    def release(self, call_id: str, now: datetime, reason: str) -> dict:
        return self._close(call_id, "released", 0, now, {"note": reason})

    def _close(self, call_id: str, state: str, actual: int, now: datetime, extra: dict) -> dict:
        usage_doc = self.store.get_doc(USAGE, call_id)
        if usage_doc is None:
            raise KeyError(f"chiamata sconosciuta: {call_id}")
        refs = [(BUDGET, usage_doc["month"]), (USAGE, call_id)]

        def _apply(docs):
            usage = docs[(USAGE, call_id)]
            if usage is None:
                raise KeyError(f"chiamata sconosciuta: {call_id}")
            if usage["state"] != "reserved":  # gia' chiusa: idempotente
                return {}, usage
            reserved = usage["reserved_micros"]
            month_doc = docs[(BUDGET, usage["month"])] or _empty_month(usage["month"])
            month_doc = _add(month_doc, usage["day"], reserved=-reserved, actual=actual)
            usage = {**usage, **extra, "state": state, "actual_micros": actual, "closed_at": iso(now),
                     "overrun": actual > reserved}
            return {(BUDGET, usage["month"]): month_doc, (USAGE, call_id): usage}, usage

        return self.store.transact(refs, _apply)

    # --- lettura -----------------------------------------------------------------------------
    def calls_for_task(self, task_id: str) -> list[dict]:
        return sorted(self.store.query_docs(USAGE, "task_id", task_id), key=lambda d: d["created_at"])

    def open_reservations(self) -> list[dict]:
        return sorted(self.store.query_docs(USAGE, "state", "reserved"), key=lambda d: d["created_at"])

    def summary(self, now: datetime) -> dict[str, Any]:
        month = self.store.get_doc(BUDGET, month_key(now)) or _empty_month(month_key(now))
        day = _day(month, day_key(now))
        return {
            "approved": self.limits.approved,
            "month": month["month"], "month_actual": month["actual"], "month_reserved": month["reserved"],
            "month_hard": self.limits.monthly_hard, "month_soft": self.limits.monthly_soft,
            "day": day_key(now), "day_actual": day["actual"], "day_reserved": day["reserved"],
            "day_hard": self.limits.daily_hard, "day_soft": self.limits.daily_soft,
            "open_reservations": len(self.open_reservations()),
        }


def _add(month_doc: dict, day: str, reserved: int = 0, actual: int = 0) -> dict:
    month_doc = {**month_doc, "days": dict(month_doc["days"])}
    current = _day(month_doc, day)
    current["reserved"] = max(0, current["reserved"] + reserved)
    current["actual"] += actual
    month_doc["days"][day] = current
    month_doc["reserved"] = max(0, month_doc["reserved"] + reserved)
    month_doc["actual"] += actual
    return month_doc


def _block_reason(month_doc: dict, day: str, amount: int, limits: BudgetLimits) -> Optional[str]:
    current = _day(month_doc, day)
    day_total = current["reserved"] + current["actual"] + amount
    month_total = month_doc["reserved"] + month_doc["actual"] + amount
    if day_total > limits.daily_hard:
        return (f"tetto giornaliero: {micros_to_usd(day_total):.4f} > {micros_to_usd(limits.daily_hard):.2f} USD "
                "(rinviare o chiedere un'eccezione specifica)")
    if month_total > limits.monthly_hard:
        return f"tetto mensile: {micros_to_usd(month_total):.4f} > {micros_to_usd(limits.monthly_hard):.2f} USD"
    return None


def _soft_warning(month_doc: dict, day: str, limits: BudgetLimits) -> str:
    current = _day(month_doc, day)
    warnings = []
    if current["reserved"] + current["actual"] > limits.daily_soft:
        warnings.append("soglia giornaliera di attenzione superata")
    if month_doc["reserved"] + month_doc["actual"] > limits.monthly_soft:
        warnings.append("soglia mensile di attenzione superata")
    return "; ".join(warnings)
