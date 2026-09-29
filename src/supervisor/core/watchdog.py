"""Watchdog: il supervisore controlla se stesso (specifica M6, "monitoraggio del supervisore").

Un supervisore che si ferma in silenzio e' peggio di uno che non c'e': questi controlli girano in un workflow
separato e segnalano su Telegram cio' che nessun altro vedrebbe.
- ultimo giro di observe completato troppo vecchio (cron disattivato, credenziali scadute, crash ripetuti);
- ultimo giro fallito;
- lock di observe oltre la scadenza del lease;
- chiamate AI dall'esito incerto da piu' di un giorno (budget prenotato e mai riconciliato);
- notifiche rimaste `pending` (invio interrotto: forse non sono arrivate).
Nessun controllo scrive nello stato: il watchdog legge e basta.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from supervisor.core.budget import BudgetLedger
from supervisor.core.clock import hours_between, iso, parse_iso
from supervisor.state.store import StateStore


@dataclass(frozen=True)
class Problem:
    key: str
    text: str


def check(store: StateStore, now: datetime, max_age_hours: float, ledgers: tuple[BudgetLedger, ...] = ()) -> list[Problem]:
    problems: list[Problem] = []
    run = store.last_run()
    if run is None:
        problems.append(Problem("no_run", "Observe non ha mai completato un giro"))
    else:
        age = hours_between(run.finished_at or run.started_at, now)
        if age > max_age_hours:
            problems.append(Problem("stale", f"L'ultimo giro di Observe risale a {age:.0f} ore fa "
                                             f"(limite {max_age_hours:g}): i cron potrebbero essere fermi"))
        if run.status == "failed":
            problems.append(Problem("failed", f"L'ultimo giro di Observe e' fallito: {run.error[:160]}"))
    lock = store.get_lock("observe")
    if lock and parse_iso(lock["lease_until"]) < now - timedelta(minutes=30):
        problems.append(Problem("lock", f"Lock di Observe scaduto e mai rilasciato (owner {lock['owner']})"))
    day_ago = iso(now - timedelta(days=1))
    for ledger in ledgers:
        stale = [u for u in ledger.open_reservations() if u["created_at"] < day_ago]
        if stale:
            where = "evaluation" if ledger.namespace == "eval" else "operativo"
            problems.append(Problem(f"reservations-{ledger.namespace or 'main'}",
                                    f"{len(stale)} chiamate AI da riconciliare da oltre un giorno (budget {where})"))
    pending = [n for n in store.pending_notifications() if (n.get("claimed_at") or "") < iso(now - timedelta(hours=1))]
    if pending:
        problems.append(Problem("pending", f"{len(pending)} notifiche con invio interrotto: controlla se sono arrivate"))
    return problems


def message(problems: list[Problem], details: str = "", test: bool = False) -> Optional[str]:
    from supervisor.reporting.messages import esc, link

    if not problems:
        if not test:
            return None
        text = "<b>🐕 Watchdog GTP</b>\n✅ Tutto regolare: Observe gira e lo stato e' coerente."
    else:
        text = "<b>🐕 Watchdog GTP · qualcosa non va</b>\n" + "\n".join(f"⚠️ {esc(p.text)}" for p in problems)
    if details:
        text += "\n" + link(details, "📄 Apri la run")
    return text
