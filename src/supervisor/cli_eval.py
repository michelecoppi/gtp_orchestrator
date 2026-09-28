"""Comandi `python -m supervisor eval ...`: confronto dei modelli (specifica, sez. 14).

- `eval prepare`  calcola per ogni caso storico base, test nascosti, FAIL_TO_PASS e PASS_TO_PASS (Docker,
                  nessun costo AI) e scrive `evals/engineering/cases.lock.json`;
- `eval run`      esegue i candidati sui casi con il budget dedicato `[evaluation]` di config/budget.toml,
                  contabilizzato a parte (namespace `eval`); `--dry-run` stima i costi senza chiamate.
Il report confronta i modelli; la scelta resta una modifica manuale di config/routing.toml.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from supervisor.cli import _now
from supervisor.collectors.github import GitHubApi
from supervisor.collectors.http import RequestsHttp
from supervisor.core.budget import BudgetLedger, load_budget, micros_to_usd
from supervisor.core.config import ROOT, ConfigError, Settings
from supervisor.core.policy import load_policy
from supervisor.engineering.config import load_engineering
from supervisor.engineering.runner import DockerRunner
from supervisor.evals.engineering import load_cases, prepare_case, report, run_case, save_lock
from supervisor.evals.triage import load_triage_cases, run_triage_case, triage_report
from supervisor.llm.catalog import cost_micros, load_catalog, load_routing
from supervisor.llm.gateway import LLMBlocked, LLMGateway
from supervisor.state import open_store

ENG_CASES = ROOT / "evals" / "engineering" / "cases.toml"
ENG_LOCK = ROOT / "evals" / "engineering" / "cases.lock.json"
TRIAGE_CASES = ROOT / "evals" / "triage" / "cases.toml"
GAME = "michelecoppi/guess_the_player_from_the_path"


def _eval_gateway(settings: Settings, store) -> LLMGateway:
    from supervisor.cli import _llm_client

    budget = load_budget(settings.config_dir)
    if budget.evaluation is None:
        raise ConfigError("config/budget.toml: manca la sezione [evaluation]")
    return LLMGateway(catalog=load_catalog(settings.config_dir),
                      ledger=BudgetLedger(store, budget.evaluation, namespace="eval"), policy=load_policy(),
                      client=_llm_client(settings), ai_enabled=settings.ai_enabled, task_limits=budget.tasks,
                      allow_unverified=True)


def cmd_prepare(args, settings: Settings) -> int:
    repo = load_engineering(settings.config_dir).repos[GAME]
    api = GitHubApi(RequestsHttp(), settings.github_token)

    def fetch_issue(number: int) -> tuple[str, str]:
        issue = api.get(f"/repos/{GAME}/issues/{number}").body or {}
        return issue.get("title") or "", issue.get("body") or ""

    cases = load_cases(ENG_CASES, ENG_LOCK)
    wanted = set(args.cases.split(",")) if args.cases else None
    prepared = []
    for case in cases:
        if wanted and case.id not in wanted:
            prepared.append(case)
            continue
        print(f"preparo {case.id} (PR #{case.pr})...", flush=True)
        case = prepare_case(case, Path(args.repo_dir), repo, DockerRunner, fetch_issue)
        state = "ok" if case.usable else f"escluso: {case.note}"
        print(f"  {state} — FAIL_TO_PASS {len(case.f2p)}, PASS_TO_PASS {len(case.p2p)}", flush=True)
        prepared.append(case)
    save_lock(prepared, ENG_LOCK)
    print(f"{sum(c.usable for c in prepared)} casi utilizzabili su {len(prepared)}; scritto {ENG_LOCK.name}")
    return 0


def _estimate_engineering(settings: Settings, models: list[str], n_cases: int, repeat: int) -> None:
    catalog = load_catalog(settings.config_dir)
    repo = load_engineering(settings.config_dir).repos[GAME]
    routing = load_routing(settings.config_dir)
    context_tokens = repo.max_context_files * repo.max_file_bytes // 3 + 12000
    for key in models:
        model = catalog.get(key)
        price = model.price_on(_now_date()) if model else None
        if price is None:
            print(f"{key}: modello o prezzo assente")
            continue
        per_case = (cost_micros(price, 12000, routing["engineer_plan"].max_output_tokens)
                    + 2 * cost_micros(price, context_tokens, routing["engineer_patch"].max_output_tokens))
        print(f"{key}: tetto pessimista {micros_to_usd(per_case):.3f} USD per caso, "
              f"{micros_to_usd(per_case * n_cases * repeat):.2f} USD per {n_cases} casi x {repeat}")


def _now_date():
    from supervisor.core.clock import utcnow
    from supervisor.llm.catalog import rome_date

    return rome_date(utcnow())


def cmd_run(args, settings: Settings) -> int:
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    store = open_store(settings)
    now = _now(args)
    if args.suite == "triage":
        cases = load_triage_cases(TRIAGE_CASES)
        if args.cases:
            cases = [c for c in cases if c.id in set(args.cases.split(","))]
        tokens = load_routing(settings.config_dir)["triage"].max_output_tokens
        if args.dry_run:
            gateway = _eval_gateway(settings, store)
            for key in models:
                try:
                    from supervisor.workers.triage import SYSTEM, build_prompt

                    check = gateway.preflight(key, SYSTEM, build_prompt(cases[0].finding), tokens, now)
                    total = check.amount_usd * len(cases) * args.repeat
                    print(f"{key}: prenotazione per caso {check.amount_usd:.5f} USD, totale max {total:.4f} USD"
                          + (f" — bloccato: {check.blocked}" if check.blocked else ""))
                except LLMBlocked as exc:
                    print(f"{key}: {exc}")
            return 0
        gateway = _eval_gateway(settings, store)
        runs = [asdict(run_triage_case(case, key, r, gateway, tokens, now))
                for r in range(1, args.repeat + 1) for key in models for case in cases]
        text = triage_report(runs, cases)
    else:
        cases = [c for c in load_cases(ENG_CASES, ENG_LOCK) if c.usable and c.f2p]
        if args.cases:
            cases = [c for c in cases if c.id in set(args.cases.split(","))]
        if not cases:
            print("nessun caso utilizzabile: eseguire prima `eval prepare`")
            return 1
        if args.dry_run:
            _estimate_engineering(settings, models, len(cases), args.repeat)
            return 0
        repo = load_engineering(settings.config_dir).repos[GAME]
        gateway = _eval_gateway(settings, store)
        runs = []
        for r in range(1, args.repeat + 1):
            for case in cases:
                for key in models:
                    print(f"{case.id} / {key} / ripetizione {r}...", flush=True)
                    result = asdict(run_case(case, key, r, Path(args.repo_dir), repo, gateway, DockerRunner, now))
                    print(f"  {'RISOLTO' if result['resolved'] else result['status']} — {result['cost_usd']:.4f} USD, "
                          f"{result['duration_s']:.0f}s {result['error'][:120]}", flush=True)
                    runs.append(result)
                    (out / "results.json").write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
        text = report(runs, cases)
    (out / "results.json").write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


def register(sub) -> None:
    p = sub.add_parser("eval", help="confronto dei modelli su casi storici (spec. sez. 14)")
    ev = p.add_subparsers(dest="eval_command", required=True)
    prep = ev.add_parser("prepare", help="calcola FAIL_TO_PASS/PASS_TO_PASS dei casi (Docker, nessun costo AI)")
    prep.add_argument("--repo-dir", required=True, help="clone completo del repository del gioco")
    prep.add_argument("--cases", help="solo questi id, separati da virgola")
    prep.set_defaults(fn=cmd_prepare)
    run = ev.add_parser("run", help="esegue i modelli candidati con il budget [evaluation]")
    run.add_argument("--suite", choices=("engineering", "triage"), required=True)
    run.add_argument("--models", required=True, help="chiavi del catalogo separate da virgola")
    run.add_argument("--cases", help="solo questi id, separati da virgola")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--repo-dir", default=".", help="clone completo del gioco (solo engineering)")
    run.add_argument("--out", default="out/eval")
    run.add_argument("--dry-run", action="store_true", help="stima dei costi, nessuna chiamata")
    run.add_argument("--now", help=argparse.SUPPRESS)
    run.set_defaults(fn=cmd_run)
