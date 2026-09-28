"""Evaluation del triage: stessi prompt e schema del triage reale, su casi con etichette di riferimento.

Le etichette attese (priorita', ruolo, serve Michele) sono una proposta del supervisore da far rivedere a
Michele: misurano l'aderenza a un criterio dichiarato, non una verita' assoluta. Contano anche validita'
dello schema, costo e latenza.
"""
from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from supervisor.core.models import Finding
from supervisor.llm.gateway import LLMBlocked, LLMCallFailed, LLMGateway
from supervisor.workers.triage import SCHEMA, SYSTEM, TASK, InvalidOutput, build_prompt, parse_output


@dataclass(frozen=True)
class TriageCase:
    id: str
    finding: Finding
    priority: str
    role: str
    needs_human: bool


def load_triage_cases(path: Path) -> list[TriageCase]:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    cases = []
    for item in raw.get("cases", []):
        finding = Finding(item["rule"], item["subject"], item["id"], item["statement"], item["severity"],
                          list(item.get("evidence", [])), created_at="2026-09-28T08:00:00Z")
        cases.append(TriageCase(item["id"], finding, item["expected_priority"], item["expected_role"],
                                bool(item["expected_needs_human"])))
    return cases


@dataclass
class TriageRun:
    case: str
    model: str
    repeat: int
    valid: bool = False
    priority_ok: bool = False
    role_ok: bool = False
    human_ok: bool = False
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str = ""
    answer: str = ""


def run_triage_case(case: TriageCase, model_key: str, repeat: int, gateway: LLMGateway, max_output_tokens: int,
                    now: datetime) -> TriageRun:
    task_id = f"eval-triage-{case.id}-{model_key}-r{repeat}-{now.strftime('%Y%m%dT%H%M%S')}"
    run = TriageRun(case.id, model_key, repeat)
    started = time.monotonic()
    try:
        result = gateway.call(task_id=task_id, task=TASK, model_key=model_key, system=SYSTEM,
                              prompt=build_prompt(case.finding), max_output_tokens=max_output_tokens, now=now,
                              json_schema=SCHEMA)
        run.cost_usd = result.cost_micros / 1_000_000
        data = parse_output(result.response.text)
        run.valid = True
        run.priority_ok = data["priority"] == case.priority
        run.role_ok = data["role"] == case.role
        run.human_ok = data["needs_human"] == case.needs_human
        run.answer = f"{data['priority']}/{data['role']}/{'umano' if data['needs_human'] else 'auto'}"
    except InvalidOutput as exc:
        run.error = f"output non valido: {exc}"
    except (LLMBlocked, LLMCallFailed) as exc:
        run.error = str(exc)
    run.latency_s = round(time.monotonic() - started, 2)
    return run


def triage_report(runs: list[dict[str, Any]], cases: list[TriageCase]) -> str:
    lines = ["# Evaluation triage", "",
             "Etichette di riferimento proposte dal supervisore (evals/triage/cases.toml), da rivedere da Michele.", "",
             "| Modello | Run | JSON valido | Priorita' | Ruolo | Serve Michele | Costo totale USD | Latenza media s |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for model in sorted({r["model"] for r in runs}):
        rs = [r for r in runs if r["model"] == model]
        n = len(rs) or 1

        def pct(key: str, rs=rs, n=n) -> str:
            return f"{sum(r[key] for r in rs) / n:.0%}"

        lines.append(f"| {model} | {len(rs)} | {pct('valid')} | {pct('priority_ok')} | {pct('role_ok')} | "
                     f"{pct('human_ok')} | {sum(r['cost_usd'] for r in rs):.5f} | "
                     f"{sum(r['latency_s'] for r in rs) / n:.1f} |")
    models = sorted({r["model"] for r in runs})
    lines += ["", "## Risposte per caso (atteso → risposta)", "", "| Caso | Atteso | " + " | ".join(models) + " |",
              "|---|---|" + "---|" * len(models)]
    for case in cases:
        expected = f"{case.priority}/{case.role}/{'umano' if case.needs_human else 'auto'}"
        cells = [", ".join(r["answer"] or "errore" for r in runs if r["case"] == case.id and r["model"] == m) or "—"
                 for m in models]
        lines.append(f"| {case.id} | {expected} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


