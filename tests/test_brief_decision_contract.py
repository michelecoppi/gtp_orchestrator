"""Contratto di `promo_brief_decisions` con Promo Studio (issue #14), senza rete.

`tests/contracts/promo_brief_decision.v1.json` e' la copia dello schema pubblicato da Promo
(`docs/schemas/promo_brief_decision.v1.json`); il `.lock.json` accanto registra lo SHA del commit di Promo da
cui viene. Qui si controlla che:
- la copia non sia stata modificata a mano (sha256 del lock);
- le fixture delle decisioni usate dai test del collector rispettino lo schema;
- ogni campo che il collector legge su una decisione sia nello schema (e in `DECISION_FIELDS`), e gli stati
  che il brief sa descrivere coincidano con quelli dello schema.
"""
import ast
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from supervisor import contracts
from supervisor.collectors.promo import DECISION_FIELDS
from supervisor.reporting.brief import BRIEF_LABELS
from test_promo_collector import BRIEF_DECISIONS

ROOT = Path(__file__).resolve().parents[1]
NAME = contracts.BRIEF_DECISION
SCHEMA = contracts.load_schema(NAME)
VALIDATOR = Draft202012Validator(SCHEMA)
# Senza questi il brief quotidiano non sa dire che fine ha fatto una campagna: Promo deve scriverli sempre.
REQUIRED_BY_SUPERVISOR = {"campaign_id", "status", "asked_at"}


def errors(doc: dict) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in VALIDATOR.iter_errors(doc)]


def test_la_copia_dello_schema_e_quella_registrata_nel_lock():
    lock = contracts.load_lock(NAME)
    assert lock["repo"] == "michelecoppi/promo_studio"
    assert lock["path"] == "docs/schemas/promo_brief_decision.v1.json"
    assert re.fullmatch(r"[0-9a-f]{40}", lock["commit"]), "serve lo SHA completo del commit di Promo"
    assert contracts.sha256_of(ROOT / "tests" / "contracts" / f"{NAME}.json") == lock["sha256"], (
        "copia modificata a mano: si aggiorna solo con `python -m supervisor contracts check --update`")
    Draft202012Validator.check_schema(SCHEMA)


def test_contracts_check_controlla_entrambi_gli_schemi():
    assert contracts.CONTRACTS == (contracts.PROMO_POST, NAME)
    assert set(contracts.READ_FIELDS) == set(contracts.CONTRACTS)


@pytest.mark.parametrize("doc", BRIEF_DECISIONS, ids=lambda d: d["campaign_id"])
def test_le_fixture_del_collector_rispettano_lo_schema(doc):
    assert errors(doc) == []


def test_le_fixture_coprono_gli_stati_decisi_e_in_attesa():
    assert {d["status"] for d in BRIEF_DECISIONS} >= {"asked", "used", "discarded"}
    assert any(d.get("imported_for") for d in BRIEF_DECISIONS)


def test_gli_stati_coincidono_con_lo_schema():
    assert set(SCHEMA["$defs"]["status"]["enum"]) == set(BRIEF_LABELS)


def test_ogni_campo_letto_dal_collector_e_nello_schema():
    assert set(DECISION_FIELDS) <= set(SCHEMA["properties"]), set(DECISION_FIELDS) - set(SCHEMA["properties"])
    assert REQUIRED_BY_SUPERVISOR <= set(SCHEMA["required"]), REQUIRED_BY_SUPERVISOR - set(SCHEMA["required"])


def _decision_keys() -> set[str]:
    """Le chiavi lette su una decisione (`d`) dentro `PromoCollector._briefs`."""
    tree = ast.parse((ROOT / "src" / "supervisor" / "collectors" / "promo.py").read_text(encoding="utf-8"))
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_briefs")
    keys = set()

    def is_decision(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "d"

    for node in ast.walk(function):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and is_decision(node.func.value) and node.args and isinstance(node.args[0], ast.Constant)):
            keys.add(node.args[0].value)
        elif (isinstance(node, ast.Subscript) and is_decision(node.value)
              and isinstance(node.slice, ast.Constant)):
            keys.add(node.slice.value)
    return keys


def test_decision_fields_elenca_tutti_i_campi_letti_dal_codice():
    keys = _decision_keys()
    assert {"campaign_id", "status", "decided_at"} <= keys  # la scansione funziona
    missing = keys - set(DECISION_FIELDS)
    assert not missing, f"campi letti ma non in DECISION_FIELDS: {missing}"
