"""L'unico ingresso per le chiamate AI a pagamento.

Ordine dei controlli, tutti nel codice e prima dell'invio (specifica, sez. 8-9):
1. policy: `call_paid_llm` deve valere `budget` e il budget deve essere approvato;
2. interruttore `SUP_AI_ENABLED`;
3. catalogo: modello abilitato, accesso verificato, prezzo valido oggi, input entro la fascia prevista;
4. limiti del task: numero di chiamate e nessuna chiamata precedente ancora da riconciliare;
5. prenotazione atomica della stima pessimista;
6. invio, senza retry e senza fallback;
7. consuntivo: costo reale dai token (o l'intera prenotazione se i token mancano), rilascio solo per i
   rifiuti certi, prenotazione mantenuta se l'esito e' incerto.
Se lo stato non risponde non parte nessuna chiamata: si prosegue solo con i controlli deterministici.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from supervisor.core.budget import BudgetExceeded, BudgetLedger, TaskLimits, micros_to_usd
from supervisor.core.policy import Policy
from supervisor.core.scrub import scrub
from supervisor.llm.catalog import (
    Catalog,
    ModelEntry,
    Price,
    cost_micros,
    estimate_input_tokens,
    reservation_micros,
    rome_date,
)
from supervisor.llm.client import LLMClient, LLMOutcomeUnknown, LLMRejected, LLMRequest, LLMResponse


class LLMBlocked(RuntimeError):
    """La chiamata non e' partita: nessun costo.

    `systemic` indica un blocco che vale per ogni chiamata del giro (policy, interruttore, budget,
    stato non disponibile): inutile provare le successive."""

    def __init__(self, message: str, systemic: bool = True) -> None:
        super().__init__(message)
        self.systemic = systemic


class LLMCallFailed(RuntimeError):
    """La chiamata e' partita ma non ha dato un risultato utilizzabile."""

    def __init__(self, message: str, call_id: str, needs_reconcile: bool) -> None:
        super().__init__(message)
        self.call_id = call_id
        self.needs_reconcile = needs_reconcile


@dataclass(frozen=True)
class Preflight:
    model: ModelEntry
    price: Price
    input_tokens: int
    amount_micros: int
    blocked: str = ""
    systemic: bool = False

    @property
    def amount_usd(self) -> float:
        return micros_to_usd(self.amount_micros)


@dataclass(frozen=True)
class CallResult:
    response: LLMResponse
    call_id: str
    cost_micros: int
    warning: str = ""


class LLMGateway:
    def __init__(self, *, catalog: Catalog, ledger: BudgetLedger, policy: Policy, client: Optional[LLMClient],
                 ai_enabled: bool, task_limits: TaskLimits, allow_unverified: bool = False) -> None:
        self.catalog = catalog
        self.ledger = ledger
        self.policy = policy
        self.client = client
        self.ai_enabled = ai_enabled
        self.task_limits = task_limits
        # Solo per le evaluation esplicite: l'accesso a un modello si verifica proprio cosi'.
        self.allow_unverified = allow_unverified

    def preflight(self, model_key: str, system: str, prompt: str, max_output_tokens: int, now: datetime,
                  allow_unverified: bool = False) -> Preflight:
        """Tutti i controlli senza effetti, piu' la stima: usato anche dal dry-run."""
        model = self.catalog.get(model_key)
        if model is None:
            raise LLMBlocked(f"modello '{model_key}' assente dal catalogo")
        price = model.price_on(rome_date(now))
        if price is None:
            raise LLMBlocked(f"nessun prezzo valido oggi per {model_key} (catalogo {self.catalog.version})")
        input_tokens = estimate_input_tokens(system, prompt)
        amount = reservation_micros(price, input_tokens, max_output_tokens)
        reasons: list[str] = []
        item_reasons: list[str] = []
        if self.policy.decide("call_paid_llm") != "budget":
            reasons.append(f"policy: call_paid_llm = {self.policy.decide('call_paid_llm')}")
        if not self.ai_enabled:
            reasons.append("SUP_AI_ENABLED=false")
        if not model.enabled:
            reasons.append(f"{model_key} disabilitato nel catalogo")
        if not model.access_verified and not (allow_unverified or self.allow_unverified):
            reasons.append(f"accesso a {model_key} non verificato (supervisor llm smoke {model_key})")
        if input_tokens > model.max_input_tokens:
            item_reasons.append(f"input stimato {input_tokens} token oltre il limite {model.max_input_tokens}")
        if max_output_tokens > model.max_output_tokens:
            reasons.append(f"output richiesto {max_output_tokens} oltre il limite {model.max_output_tokens}")
        try:
            budget_reason = self.ledger.check(amount, now)
        except Exception as exc:
            budget_reason = scrub(f"stato del budget non disponibile: {type(exc).__name__}: {exc}")[:200]
        if budget_reason:
            reasons.append(budget_reason)
        return Preflight(model, price, input_tokens, amount, "; ".join(reasons + item_reasons), systemic=bool(reasons))

    def call(self, *, task_id: str, task: str, model_key: str, system: str, prompt: str, max_output_tokens: int,
             now: datetime, json_schema: Optional[dict] = None, allow_unverified: bool = False) -> CallResult:
        check = self.preflight(model_key, system, prompt, max_output_tokens, now, allow_unverified)
        if check.blocked:
            raise LLMBlocked(check.blocked, systemic=check.systemic)
        if self.client is None:
            raise LLMBlocked("nessun client LLM configurato")
        try:
            previous = self.ledger.calls_for_task(task_id)
        except Exception as exc:
            raise LLMBlocked(scrub(f"stato non disponibile: {type(exc).__name__}: {exc}")[:200]) from exc
        if any(c["state"] == "reserved" for c in previous):
            raise LLMBlocked(f"il task {task_id} ha una chiamata dall'esito incerto: riconciliarla prima", systemic=False)
        if len(previous) >= self.task_limits.max_llm_calls_per_task:
            raise LLMBlocked(f"limite di {self.task_limits.max_llm_calls_per_task} chiamate per task raggiunto",
                             systemic=False)
        call_id = f"{task_id}#{len(previous) + 1}"
        try:
            reservation = self.ledger.reserve(
                call_id, task_id=task_id, task=task, provider=check.model.provider, model=check.model.key,
                pricing_version=self.catalog.version, amount=check.amount_micros, now=now)
        except BudgetExceeded as exc:
            raise LLMBlocked(str(exc)) from exc
        except Exception as exc:
            raise LLMBlocked(scrub(f"prenotazione non riuscita: {type(exc).__name__}: {exc}")[:200]) from exc
        if reservation.existing:
            raise LLMBlocked(f"chiamata {call_id} gia' prenotata (stato {reservation.state})", systemic=False)

        request = LLMRequest(model=check.model.litellm_model, task=task, system=system, prompt=prompt,
                             max_output_tokens=max_output_tokens, json_schema=json_schema)
        try:
            response = self.client.complete(request)
        except LLMRejected as exc:
            self.ledger.release(call_id, now, f"rifiutata dal provider: {exc}")
            raise LLMCallFailed(f"rifiutata dal provider: {exc}", call_id, needs_reconcile=False) from exc
        except (LLMOutcomeUnknown, Exception) as exc:
            # Esito incerto: la prenotazione resta finche' non si riconcilia.
            message = scrub(f"{type(exc).__name__}: {exc}")[:300]
            raise LLMCallFailed(f"esito incerto, prenotazione mantenuta: {message}", call_id,
                                needs_reconcile=True) from exc

        if response.input_tokens or response.output_tokens:
            actual = cost_micros(check.price, response.input_tokens, response.output_tokens)
            note = ""
        else:
            actual, note = check.amount_micros, "usage assente: addebitata l'intera prenotazione"
        self.ledger.settle(call_id, actual, now, response.input_tokens, response.output_tokens, note)
        return CallResult(response, call_id, actual, reservation.warning)
