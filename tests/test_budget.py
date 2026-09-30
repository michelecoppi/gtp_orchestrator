"""Il tetto di spesa regge a concorrenza, timeout e crash (criterio di uscita di M2)."""
import threading
from datetime import timedelta

import pytest

from ai_factory import write_ai_config
from conftest import make_store
from supervisor.core.budget import (
    BudgetExceeded,
    BudgetLedger,
    BudgetLimits,
    load_budget,
    micros_to_usd,
    usd_to_micros,
)
from supervisor.core.clock import parse_iso
from supervisor.core.config import ConfigError

NOW = parse_iso("2026-09-28T08:00:00Z")
LIMITS = BudgetLimits(daily_soft=usd_to_micros(0.5), daily_hard=usd_to_micros(1.0),
                      monthly_soft=usd_to_micros(10), monthly_hard=usd_to_micros(15), approved=True)


def reserve(ledger, call_id, usd, now=NOW):
    return ledger.reserve(call_id, task_id=call_id.split("#")[0], task="triage", provider="openai", model="m",
                          pricing_version="v", amount=usd_to_micros(usd), now=now)


def test_prenota_consuntiva_e_libera_la_differenza(store):
    ledger = BudgetLedger(store, LIMITS)
    r = reserve(ledger, "t1#1", 0.40)
    assert r.amount == 400_000 and not r.existing and r.warning == ""
    ledger.settle("t1#1", usd_to_micros(0.10), NOW, input_tokens=1000, output_tokens=50)
    s = ledger.summary(NOW)
    assert (s["day_actual"], s["day_reserved"], s["month_actual"], s["month_reserved"]) == (100_000, 0, 100_000, 0)
    ledger.settle("t1#1", usd_to_micros(0.30), NOW)  # gia' chiusa: idempotente, nessun doppio addebito
    assert ledger.summary(NOW)["month_actual"] == 100_000


def test_stessa_chiamata_non_si_prenota_due_volte(store):
    ledger = BudgetLedger(store, LIMITS)
    reserve(ledger, "t1#1", 0.40)
    again = reserve(ledger, "t1#1", 0.40)
    assert again.existing and ledger.summary(NOW)["day_reserved"] == 400_000


def test_tetto_giornaliero_e_mensile(store):
    ledger = BudgetLedger(store, LIMITS)
    reserve(ledger, "a#1", 0.60)
    with pytest.raises(BudgetExceeded, match="giornaliero"):
        reserve(ledger, "b#1", 0.50)
    assert store.get_doc("usage", "b#1") is None  # nessuna traccia di una prenotazione rifiutata
    # L'esempio della specifica: un'escalation Opus da 1,20 USD non passa il tetto di 1 USD al giorno.
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "opus#1", 1.20, NOW + timedelta(days=1))
    monthly = BudgetLedger(store, BudgetLimits(usd_to_micros(0.5), usd_to_micros(1), usd_to_micros(0.5),
                                               usd_to_micros(1.0), approved=True))
    with pytest.raises(BudgetExceeded, match="mensile"):
        reserve(monthly, "c#1", 0.50, NOW + timedelta(days=2))


def test_soglia_di_attenzione(store):
    ledger = BudgetLedger(store, LIMITS)
    assert "giornaliera" in reserve(ledger, "a#1", 0.60).warning


def test_budget_non_approvato(store):
    ledger = BudgetLedger(store, BudgetLimits(1, 2, 3, 4, approved=False))
    with pytest.raises(BudgetExceeded, match="non approvato"):
        reserve(ledger, "a#1", 0.000001)
    assert "non approvato" in ledger.check(1, NOW)


def test_esito_incerto_resta_prenotato_finche_non_si_riconcilia(store):
    ledger = BudgetLedger(store, LIMITS)
    reserve(ledger, "t#1", 0.40)
    # Il processo muore prima della risposta: la prenotazione continua a contare.
    assert [u["call_id"] for u in ledger.open_reservations()] == ["t#1"]
    with pytest.raises(BudgetExceeded):
        reserve(ledger, "u#1", 0.70)
    ledger.release("t#1", NOW, "verificato: non addebitata")
    assert ledger.open_reservations() == [] and ledger.summary(NOW)["day_reserved"] == 0
    reserve(ledger, "u#1", 0.70)


def test_superamento_della_stima_registrato(store):
    ledger = BudgetLedger(store, LIMITS)
    reserve(ledger, "t#1", 0.10)
    usage = ledger.settle("t#1", usd_to_micros(0.15), NOW)
    assert usage["overrun"] and ledger.summary(NOW)["day_actual"] == 150_000


def test_giorno_e_mese_di_roma():
    from supervisor.core.budget import day_key, month_key

    late = parse_iso("2026-09-30T22:30:00Z")  # 00:30 del 1 ottobre a Roma
    assert day_key(late) == "2026-10-01" and month_key(late) == "2026-10"


@pytest.mark.parametrize("kind", ["sqlite", pytest.param("firestore", marks=pytest.mark.firestore)])
def test_prenotazioni_concorrenti_non_superano_il_tetto(kind, tmp_path):
    first = make_store(kind, tmp_path)
    if kind == "sqlite":
        stores = [first] + [make_store(kind, tmp_path) for _ in range(7)]
    else:  # stesso prefisso, client diversi: transazioni davvero concorrenti
        from google.cloud import firestore

        from supervisor.state.firestore import FirestoreStore
        stores = [first] + [FirestoreStore(firestore.Client(project="demo-gtp-supervisor"), prefix=first.prefix)
                            for _ in range(7)]
    barrier = threading.Barrier(len(stores))
    outcomes: list[str] = []

    def contend(i):
        ledger = BudgetLedger(stores[i], LIMITS)
        barrier.wait()
        try:
            reserve(ledger, f"t{i}#1", 0.30)
            outcomes.append("ok")
        except BudgetExceeded:
            outcomes.append("tetto")
        except Exception:  # contesa oltre i tentativi della transazione: nessuna prenotazione
            outcomes.append("errore")

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(len(stores))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    summary = BudgetLedger(first, LIMITS).summary(NOW)
    assert outcomes.count("ok") <= 3
    assert summary["day_reserved"] == outcomes.count("ok") * 300_000 <= LIMITS.daily_hard
    if kind == "sqlite":
        assert outcomes.count("ok") == 3


def test_config_budget(tmp_path):
    config = write_ai_config(tmp_path, approved=False)
    budget = load_budget(config)
    assert not budget.limits.approved and budget.limits.daily_hard == 1_000_000
    assert budget.tasks.max_llm_calls_per_task == 8
    (config / "budget.toml").write_text('daily_soft_limit = 2\ndaily_hard_limit = 1\nmonthly_soft_limit = 1\n'
                                        'monthly_hard_limit = 15\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="incoerenti"):
        load_budget(config)


def test_budget_versionato_valido_e_approvato():
    budget = load_budget("config")
    assert budget.limits.approved and budget.limits.approved_by == "Michele Coppi"
    assert micros_to_usd(budget.limits.monthly_hard) == 15.0 and micros_to_usd(budget.limits.daily_hard) == 1.0


def test_cli_riconcilia_una_chiamata_addebitata(tmp_path, monkeypatch, capsys):
    """Il caso del workflow Budget `reconcile`: chiamata interrotta ma addebitata dal provider."""
    from supervisor import cli
    from supervisor.state.sqlite import SQLiteStore
    from supervisor.state.store import USAGE

    db = tmp_path / "s.sqlite3"
    monkeypatch.setenv("SUP_STORE", "sqlite")
    monkeypatch.setenv("SUP_SQLITE_PATH", str(db))
    ledger = BudgetLedger(SQLiteStore(str(db)), LIMITS, namespace="eval")
    reserve(ledger, "eval-x#3", 0.25)
    base = ["budget", "--namespace", "eval", "--now", "2026-09-28T09:00:00Z"]

    assert cli.main([*base, "--reconcile", "eval-x#3"]) == 2
    assert cli.main([*base, "--reconcile", "eval-x#3", "--actual", "-1"]) == 2
    assert cli.main([*base, "--reconcile", "sconosciuta#1", "--actual", "0.1"]) == 2
    assert "chiamata sconosciuta" in capsys.readouterr().out
    assert cli.main([*base, "--reconcile", "eval-x#3", "--actual", "0.31", "--reason", "OpenRouter activity"]) == 0
    assert "settled, costo 0.3100 USD" in capsys.readouterr().out

    usage = SQLiteStore(str(db)).get_doc(USAGE, "eval-x#3")
    assert usage["overrun"] and usage["note"].endswith(": OpenRouter activity")
    assert ledger.open_reservations() == []
    assert cli.main([*base, "--reconcile", "eval-x#3", "--actual", "0.5"]) == 0  # rilanciata: idempotente
    assert micros_to_usd(ledger.summary(NOW)["month_actual"]) == pytest.approx(0.31)
