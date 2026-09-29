"""M6: il supervisore controlla se stesso; i documenti operativi scadono da soli su Firestore."""
from datetime import timedelta

import pytest

from supervisor.core import watchdog
from supervisor.core.budget import BudgetLedger, BudgetLimits, usd_to_micros
from supervisor.core.clock import iso, parse_iso
from supervisor.core.models import Event, Finding, Run
from supervisor.state.store import MemoryStore

NOW = parse_iso("2026-09-29T12:00:00Z")
LIMITS = BudgetLimits(usd_to_micros(0.5), usd_to_micros(1), usd_to_micros(10), usd_to_micros(15), approved=True)


def healthy_store():
    store = MemoryStore()
    store.save_run(Run(id="r1", started_at=iso(NOW - timedelta(hours=1)), finished_at=iso(NOW - timedelta(hours=1)),
                       status="completed"))
    return store


def test_tutto_regolare_nessun_messaggio():
    store = healthy_store()
    assert watchdog.check(store, NOW, 4, (BudgetLedger(store, LIMITS),)) == []
    assert watchdog.message([]) is None
    assert "Tutto regolare" in watchdog.message([], test=True)


def test_supervisore_fermo_o_fallito():
    store = MemoryStore()
    assert [p.key for p in watchdog.check(store, NOW, 4)] == ["no_run"]
    store.save_run(Run(id="r1", started_at=iso(NOW - timedelta(hours=9)), finished_at=iso(NOW - timedelta(hours=9)),
                       status="failed", error="GitHubError: HTTP 401 Bad credentials"))
    problems = {p.key: p.text for p in watchdog.check(store, NOW, 4)}
    assert "9 ore fa" in problems["stale"] and "HTTP 401" in problems["failed"]
    text = watchdog.message(list(watchdog.check(store, NOW, 4)), "https://github.com/o/r/actions/runs/9")
    assert text.startswith("<b>🐕 Watchdog GTP · qualcosa non va</b>") and "Apri la run" in text


def test_lock_prenotazioni_e_notifiche_rimaste_appese():
    store = healthy_store()
    store.acquire_lock("observe", "run-morto", 900, NOW - timedelta(hours=2))
    ledger = BudgetLedger(store, LIMITS)
    ledger.reserve("t#1", task_id="t", task="triage", provider="p", model="m", pricing_version="v",
                   amount=1000, now=NOW - timedelta(days=2))
    eval_ledger = BudgetLedger(store, LIMITS, namespace="eval")
    eval_ledger.reserve("e#1", task_id="e", task="triage", provider="p", model="m", pricing_version="v",
                        amount=1000, now=NOW - timedelta(hours=2))  # recente: non ancora un problema
    store.claim_notification("brief-x", iso(NOW - timedelta(hours=3)), "testo")
    keys = [p.key for p in watchdog.check(store, NOW, 4, (ledger, eval_ledger))]
    assert keys == ["lock", "reservations-main", "pending"]


@pytest.mark.firestore
def test_firestore_scrive_la_scadenza(tmp_path):
    from conftest import make_store

    store = make_store("firestore", tmp_path)
    event = Event("github:o/r", "issue", "1", "open", "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z")
    store.commit_stream("s#x", [event], None)
    doc = store._col("events").document(event.dedupe_key).get().to_dict()
    assert doc["expire_at"].date().isoformat() == "2027-11-03"  # 400 giorni dalla ricezione
    store.save_run(Run(id="r1", started_at="2026-09-29T11:00:00Z", status="completed"))
    assert store.last_run().id == "r1"  # il Timestamp non rompe la lettura del modello
    finding = Finding("ci_failed", "o/r", "k", "x", "alta", created_at="2026-09-29T11:00:00Z")
    store.add_findings([finding])
    store.resolve_findings([finding.id], "2026-09-29T12:00:00Z")
    assert store.findings_since("")[0].resolved_at == "2026-09-29T12:00:00Z"
    assert store.get_doc("findings", finding.id)["expire_at"].year == 2027



def test_pulizia_dei_documenti_scaduti(store):
    old = NOW - timedelta(days=500)
    for i, when in enumerate((old, NOW)):
        event = Event("github:o/r", "issue", str(i), "open", iso(when), iso(when))
        store.commit_stream("s#x", [event], None)
    store.save_run(Run(id="vecchio", started_at=iso(NOW - timedelta(days=100)), status="completed"))
    store.save_run(Run(id="nuovo", started_at=iso(NOW), status="completed"))
    store.claim_notification("n-vecchia", iso(NOW - timedelta(days=40)), "x")
    finding = Finding("ci_failed", "o/r", "k", "x", "alta", created_at=iso(NOW - timedelta(days=800)))
    store.add_findings([finding])
    store.resolve_findings([finding.id], iso(NOW - timedelta(days=400)))
    open_finding = Finding("ci_failed", "o/r", "aperto", "x", "alta", created_at=iso(NOW - timedelta(days=800)))
    store.add_findings([open_finding])
    removed = store.prune_expired(NOW)
    assert removed["events"] == 1 and removed["runs"] == 1 and removed["notifications"] == 1
    assert removed["findings"] == 1  # solo il finding risolto da oltre un anno
    assert [e.source_id for e in store.events_since("")] == ["1"] and store.last_run().id == "nuovo"
    assert [f.key for f in store.open_findings()] == ["aperto"]
    assert store.prune_expired(NOW) == {k: 0 for k in removed}  # idempotente
