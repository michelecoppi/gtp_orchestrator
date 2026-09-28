"""Configurazione AI di prova: un modello a prezzo tondo, budget e policy regolabili."""
from __future__ import annotations

from pathlib import Path

from supervisor.core.budget import BudgetLedger, load_budget
from supervisor.core.policy import Policy
from supervisor.llm.catalog import load_catalog
from supervisor.llm.gateway import LLMGateway


def write_ai_config(tmp_path: Path, *, approved: bool = True, verified: bool = True, enabled: bool = True,
                    daily_hard: float = 1.0, monthly_hard: float = 15.0, price_until: str = "",
                    max_calls: int = 8, extra_models: tuple[str, ...] = ()) -> Path:
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    until = f', until = "{price_until}"' if price_until else ""
    entries = [f'''
[[models]]
key = "{key}"
provider = "openai"
litellm_model = "openai/{key}"
enabled = {str(enabled).lower()}
access_verified = {str(verified).lower()}
max_input_tokens = 5000
max_output_tokens = 10000
prices = [{{ from = "2026-01-01"{until}, input = 1.00, output = 2.00 }}]
''' for key in ("test-model", *extra_models)]
    (config / "models.toml").write_text('version = "test-1"\n' + "".join(entries), encoding="utf-8")
    (config / "budget.toml").write_text(f'''
approved = {str(approved).lower()}
approved_by = "Michele Coppi"
approved_on = "2026-09-28"
currency = "USD"
daily_soft_limit = {min(0.5, daily_hard)}
daily_hard_limit = {daily_hard}
monthly_soft_limit = {min(10.0, monthly_hard)}
monthly_hard_limit = {monthly_hard}
[limits]
max_llm_calls_per_task = {max_calls}
''', encoding="utf-8")
    (config / "routing.toml").write_text('''
[tasks.triage]
model = "test-model"
max_output_tokens = 500
max_items_per_run = 10
''', encoding="utf-8")
    return config


def gateway(store, config: Path, client, *, ai_enabled: bool = True, paid: str = "budget") -> LLMGateway:
    budget = load_budget(config)
    return LLMGateway(catalog=load_catalog(config), ledger=BudgetLedger(store, budget.limits),
                      policy=Policy(2, "test", {"call_paid_llm": paid}), client=client, ai_enabled=ai_enabled,
                      task_limits=budget.tasks)
