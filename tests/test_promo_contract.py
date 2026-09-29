"""Contratto di `promo_posts` con Promo Studio (issue #11), senza rete.

`tests/contracts/promo_post.v1.json` e' la copia dello schema pubblicato da Promo
(`docs/schemas/promo_post.v1.json`); il `.lock.json` accanto registra lo SHA del commit di Promo da cui
viene. Qui si controlla che:
- la copia non sia stata modificata a mano (sha256 del lock);
- le fixture del supervisore (quella di replay e quelle dei test del collector) rispettino lo schema;
- ogni campo che il collector legge sia descritto nello schema, e gli stati coincidano.
Se Promo cambia lo schema, `python -m supervisor contracts check` lo segnala (passo non bloccante di
growth.yml) e `--update` riscrive la copia: da li' questi test dicono cosa va riallineato.
"""
import ast
import base64
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from factory import post as factory_post
from supervisor import contracts
from supervisor.collectors.http import FixtureHttp
from supervisor.collectors.promo import HISTORY_FIELDS, POST_FIELDS, STATUSES
from test_promo_collector import YESTERDAY
from test_promo_collector import post as collector_post

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "tests" / "contracts"
SCHEMA = contracts.load_schema()
VALIDATOR = Draft202012Validator(SCHEMA)
# Campi su cui si reggono le regole del supervisore: Promo deve scriverli sempre.
REQUIRED_BY_SUPERVISOR = {"id", "status", "created_at", "created_for", "scheduled_for", "published_at",
                          "history", "attempts", "error"}


def errors(doc: dict) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in VALIDATOR.iter_errors(doc)]


def test_la_copia_dello_schema_e_quella_registrata_nel_lock():
    lock = contracts.load_lock()
    assert lock["repo"] == "michelecoppi/promo_studio"
    assert lock["path"] == "docs/schemas/promo_post.v1.json"
    assert re.fullmatch(r"[0-9a-f]{40}", lock["commit"]), "serve lo SHA completo del commit di Promo"
    assert contracts.sha256_of(CONTRACTS / "promo_post.v1.json") == lock["sha256"], (
        "copia modificata a mano: si aggiorna solo con `python -m supervisor contracts check --update`")
    Draft202012Validator.check_schema(SCHEMA)


def test_la_fixture_di_replay_rispetta_lo_schema():
    posts = json.loads((ROOT / "tests" / "fixtures" / "promo_posts.json").read_text(encoding="utf-8"))
    assert posts
    for doc in posts:
        assert errors(doc) == [], doc["id"]


@pytest.mark.parametrize("status", STATUSES)
def test_le_fixture_dei_test_rispettano_lo_schema(status):
    assert errors(factory_post("p", status)) == []
    assert errors(collector_post("c", "2026-09-29", status)) == []


def test_le_fixture_del_collector_rispettano_lo_schema():
    docs = YESTERDAY + [collector_post("t2", "2026-09-29", "published", "2026-09-28T22:30:00Z")]
    for doc in docs:
        assert errors(doc) == [], doc["id"]


def test_gli_stati_coincidono_con_lo_schema():
    assert tuple(SCHEMA["$defs"]["status"]["enum"]) == STATUSES


def test_ogni_campo_letto_dal_collector_e_nello_schema():
    properties = SCHEMA["properties"]
    assert set(POST_FIELDS) <= set(properties), set(POST_FIELDS) - set(properties)
    history = properties["history"]["items"]
    assert set(HISTORY_FIELDS) <= set(history["properties"])
    assert REQUIRED_BY_SUPERVISOR <= set(SCHEMA["required"]), REQUIRED_BY_SUPERVISOR - set(SCHEMA["required"])
    assert "at" in history["required"]


def _read_fields() -> tuple[set[str], set[str]]:
    """Le chiavi lette dal codice del collector su un post (`post`/`p`) e su una voce di `history`."""
    tree = ast.parse((ROOT / "src" / "supervisor" / "collectors" / "promo.py").read_text(encoding="utf-8"))
    post_keys, history_keys = set(), set()

    def target(node: ast.AST) -> str:
        if isinstance(node, ast.Name) and node.id in ("post", "p"):
            return "post"
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "history"):
            return "history"
        return ""

    for node in ast.walk(tree):
        key, owner = None, ""
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and node.args and isinstance(node.args[0], ast.Constant)):
            key, owner = node.args[0].value, target(node.func.value)
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            key, owner = node.slice.value, target(node.value)
        if isinstance(key, str) and owner:
            (post_keys if owner == "post" else history_keys).add(key)
    return post_keys, history_keys


def test_post_fields_elenca_tutti_i_campi_letti_dal_codice():
    post_keys, history_keys = _read_fields()
    assert {"status", "created_for", "scheduled_for", "published_at"} <= post_keys  # la scansione funziona
    assert post_keys <= set(POST_FIELDS), f"campi letti ma non in POST_FIELDS: {post_keys - set(POST_FIELDS)}"
    assert history_keys <= set(HISTORY_FIELDS), history_keys - set(HISTORY_FIELDS)


# --- `contracts check`, con risposte GitHub registrate --------------------------------------------------

REPO = "/repos/michelecoppi/promo_studio"


def github(content: bytes, commit: str, names=("promo_post.v1.json",)) -> FixtureHttp:
    return FixtureHttp({
        f"GET {REPO}/contents/docs/schemas/promo_post.v1.json?ref=main": {
            "body": {"encoding": "base64", "content": base64.b64encode(content).decode()}},
        f"GET {REPO}/commits?path=docs%2Fschemas%2Fpromo_post.v1.json&per_page=1&sha=main": {
            "body": [{"sha": commit}]},
        f"GET {REPO}/contents/docs/schemas?ref=main": {"body": [{"name": n} for n in names]},
    })


@pytest.fixture
def copy_dir(tmp_path):
    for name in ("promo_post.v1.json", "promo_post.v1.lock.json"):
        (tmp_path / name).write_bytes((CONTRACTS / name).read_bytes())
    return tmp_path


def test_check_allineato(copy_dir):
    lock = contracts.load_lock(directory=copy_dir)
    content = (copy_dir / "promo_post.v1.json").read_bytes()
    result = contracts.check(github(content, lock["commit"]), directory=copy_dir)
    assert result.ok and result.lines[0].startswith("allineato")
    # Stesso contenuto, commit diverso (per esempio dopo il merge in Promo): ok, e --update aggiorna lo SHA.
    result = contracts.check(github(content, "c" * 40), directory=copy_dir, update=True)
    assert result.ok and contracts.load_lock(directory=copy_dir)["commit"] == "c" * 40


def test_check_cambiato_e_update(copy_dir):
    schema = json.loads((copy_dir / "promo_post.v1.json").read_bytes())
    schema["required"].remove("created_for")
    changed = json.dumps(schema, indent=2).encode()
    result = contracts.check(github(changed, "d" * 40), directory=copy_dir)
    assert not result.ok and result.lines[0].startswith("cambiato")
    assert "created_for" in contracts.load_schema(directory=copy_dir)["required"]  # senza --update non tocca
    result = contracts.check(github(changed, "d" * 40), directory=copy_dir, update=True)
    lock = contracts.load_lock(directory=copy_dir)
    assert lock["commit"] == "d" * 40
    assert lock["sha256"] == contracts.sha256_of(copy_dir / "promo_post.v1.json")
    assert "created_for" not in contracts.load_schema(directory=copy_dir)["required"]


def test_check_versione_nuova_e_rete_assente(copy_dir):
    lock = contracts.load_lock(directory=copy_dir)
    content = (copy_dir / "promo_post.v1.json").read_bytes()
    names = ("promo_post.v1.json", "promo_post.v2.json")
    result = contracts.check(github(content, lock["commit"], names), directory=copy_dir)
    assert not result.ok and "promo_post.v2.json" in result.lines[0]
    offline = contracts.check(FixtureHttp({}), directory=copy_dir)
    assert not offline.ok and offline.lines[0].startswith("non verificabile")
