import json
from dataclasses import asdict
from pathlib import Path

from ai_factory import gateway, write_ai_config
from supervisor.core.budget import BudgetLedger, BudgetLimits, load_budget, usd_to_micros
from supervisor.core.clock import parse_iso
from supervisor.evals.engineering import Case, load_cases, report, save_lock, summarize
from supervisor.evals.triage import load_triage_cases, run_triage_case, triage_report
from supervisor.llm.client import FakeLLM
from supervisor.state.store import MemoryStore

NOW = parse_iso("2026-09-28T08:00:00Z")
ROOT = Path(__file__).resolve().parents[1]
LIMITS = BudgetLimits(usd_to_micros(0.5), usd_to_micros(1), usd_to_micros(10), usd_to_micros(15), approved=True)


def test_budget_delle_evaluation_separato_da_quello_operativo():
    store = MemoryStore()
    main, ev = BudgetLedger(store, LIMITS), BudgetLedger(store, LIMITS, namespace="eval")
    ev.reserve("eval-x#1", task_id="eval-x", task="t", provider="p", model="m", pricing_version="v",
               amount=usd_to_micros(0.9), now=NOW)
    assert main.summary(NOW)["day_reserved"] == 0 and ev.summary(NOW)["day_reserved"] == 900_000
    assert main.open_reservations() == [] and len(ev.open_reservations()) == 1
    ev.settle("eval-x#1", usd_to_micros(0.2), NOW)
    assert ev.summary(NOW)["day_actual"] == 200_000 and main.summary(NOW)["day_actual"] == 0


def test_budget_evaluation_versionato_e_approvato():
    budget = load_budget(ROOT / "config")
    assert budget.evaluation is not None and budget.evaluation.approved
    assert budget.evaluation.monthly_hard == usd_to_micros(10)


def test_casi_versionati_e_lock(tmp_path):
    cases = load_cases(ROOT / "evals" / "engineering" / "cases.toml")
    assert len(cases) >= 8 and len({c.id for c in cases}) == len(cases)
    lock = tmp_path / "lock.json"
    prepared = [Case(**{**asdict(cases[0]), "base_sha": "b" * 40, "f2p": ["t::a"], "p2p": ["t::b"]})]
    save_lock(prepared, lock)
    again = load_cases(ROOT / "evals" / "engineering" / "cases.toml", lock)
    assert again[0].f2p == ["t::a"] and again[0].base_sha == "b" * 40 and again[1].f2p == []


def test_triage_eval_con_modello_finto(tmp_path):
    cases = load_triage_cases(ROOT / "evals" / "triage" / "cases.toml")
    assert len(cases) >= 10
    answer = json.dumps({"priority": "alta", "role": "engineering", "summary": "CI rossa.",
                         "next_step": "Leggere il log.", "needs_human": False})
    store = MemoryStore()
    gw = gateway(store, write_ai_config(tmp_path, verified=False), FakeLLM({"triage": answer}, 200, 60))
    gw.allow_unverified = True  # come nelle evaluation: si misura proprio l'accesso
    runs = [asdict(run_triage_case(c, "test-model", 1, gw, 600, NOW)) for c in cases[:3]]
    assert all(r["valid"] for r in runs) and runs[0]["priority_ok"] and runs[0]["role_ok"]
    text = triage_report(runs, cases[:3])
    assert "| test-model | 3 | 100% |" in text and "ci-main-rossa" in text


def test_report_engineering():
    cases = [Case("a", 1, 2, "f", f2p=["x"]), Case("b", 3, 4, "g", f2p=["y", "z"])]
    results = [
        {"case": "a", "model": "m1", "repeat": 1, "status": "patch_ready", "resolved": True, "cost_usd": 0.1,
         "duration_s": 100, "lines": 10, "p2p_broken": 0, "error": ""},
        {"case": "b", "model": "m1", "repeat": 1, "status": "patch_ready", "resolved": False, "cost_usd": 0.2,
         "duration_s": 200, "lines": 30, "p2p_broken": 1, "error": ""},
        {"case": "a", "model": "m2", "repeat": 1, "status": "failed", "resolved": False, "cost_usd": 0.05,
         "duration_s": 50, "lines": 0, "p2p_broken": 0, "error": "nessuna patch valida"},
    ]
    summary = summarize(results)
    assert summary["m1"]["resolved"] == 1 and round(summary["m1"]["cost"], 2) == 0.3
    text = report(results, cases)
    assert "| m1 | 2 | 1 (50%) | 2 | 1 |" in text and "| a (PR #1) | 1 | ✅ | ❌ |" in text
    assert "nessuna patch valida" in text
