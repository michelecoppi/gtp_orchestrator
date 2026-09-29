"""Contratti con gli altri repository: `python -m supervisor contracts check [--update]`.

Oggi c'e' un solo contratto: lo schema di `promo_posts` pubblicato da Promo Studio
(`docs/schemas/promo_post.v1.json`, issue #11). Il supervisore ne tiene una copia in
`tests/contracts/` insieme a un file `.lock.json` con il repository, il percorso, lo SHA del commit di
Promo da cui viene e lo sha256 del file. I test validano le fixture contro la copia, senza rete.

`contracts check` confronta la copia con lo schema su `main` di Promo (API GitHub, sola lettura, repo
pubblico: il token e' facoltativo e serve solo per il limite di richieste) e dice se e' cambiato o se e'
comparsa una versione nuova (`promo_post.v2.json`). Non modifica nulla, salvo `--update`, che riscrive
copia e lock in locale: il cambio va poi riletto, fatto passare dai test e proposto in una PR.

Codici di uscita: 0 allineato, 1 cambiato o non verificabile.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from supervisor.collectors.http import HttpClient
from supervisor.core.scrub import scrub

API = "https://api.github.com"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "tests" / "contracts"
PROMO_POST = "promo_post.v1"
VERSIONED = re.compile(r"^(?P<name>[a-z_]+)\.v(?P<version>\d+)\.json$")


@dataclass
class Check:
    ok: bool
    lines: list[str]


def lock_path(name: str = PROMO_POST, directory: Path = CONTRACTS_DIR) -> Path:
    return directory / f"{name}.lock.json"


def load_lock(name: str = PROMO_POST, directory: Path = CONTRACTS_DIR) -> dict:
    return json.loads(lock_path(name, directory).read_text(encoding="utf-8"))


def load_schema(name: str = PROMO_POST, directory: Path = CONTRACTS_DIR) -> dict:
    return json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_lock(lock: dict, name: str, directory: Path) -> None:
    lock_path(name, directory).write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


def _headers(token: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(http: HttpClient, token: str, path: str, params: dict) -> object:
    response = http.request("GET", f"{API}{path}", params=params, headers=_headers(token))
    if response.status != 200:
        detail = response.body.get("message") if isinstance(response.body, dict) else response.body
        raise LookupError(f"GET {path}: HTTP {response.status} {scrub(str(detail))[:200]}")
    return response.body


def fetch_remote(http: HttpClient, token: str, lock: dict, ref: str = "main") -> tuple[bytes, str, list[str]]:
    """Contenuto dello schema su `ref`, ultimo commit che lo tocca e file presenti nella sua cartella."""
    repo, path = lock["repo"], lock["path"]
    body = _get(http, token, f"/repos/{repo}/contents/{path}", {"ref": ref})
    if not isinstance(body, dict) or body.get("encoding") != "base64":
        raise LookupError(f"{path}: risposta inattesa dall'API contents")
    content = base64.b64decode(body["content"])
    commits = _get(http, token, f"/repos/{repo}/commits", {"path": path, "sha": ref, "per_page": 1})
    commit = commits[0]["sha"] if isinstance(commits, list) and commits else "?"
    listing = _get(http, token, f"/repos/{repo}/contents/{path.rsplit('/', 1)[0]}", {"ref": ref})
    names = [item["name"] for item in listing if isinstance(item, dict)] if isinstance(listing, list) else []
    return content, commit, sorted(names)


def newer_versions(name: str, names: list[str]) -> list[str]:
    """I file `<nome>.vN.json` con N maggiore della versione copiata."""
    match = VERSIONED.match(f"{name}.json")
    if not match:
        return []
    base, version = match["name"], int(match["version"])
    return [n for n in names
            if (m := VERSIONED.match(n)) and m["name"] == base and int(m["version"]) > version]


def check(http: HttpClient, token: str = "", name: str = PROMO_POST, directory: Path = CONTRACTS_DIR,
          update: bool = False, ref: str = "main") -> Check:
    lock = load_lock(name, directory)
    local_path = directory / f"{name}.json"
    source = f"{lock['repo']}/{lock['path']}"
    try:
        content, commit, names = fetch_remote(http, token, lock, ref)
    except Exception as exc:  # rete, limite di richieste, file rimosso
        error = scrub(f"{type(exc).__name__}: {exc}")[:300]
        return Check(False, [f"non verificabile: {source} su {ref} ({error})"])
    ok, lines = True, []
    newer = newer_versions(name, names)
    if newer:
        ok = False
        lines.append(f"versione nuova in Promo: {', '.join(newer)} "
                     "(serve un'issue e un riallineamento del collector)")
    here, there = lock["commit"][:12], commit[:12]
    if json.loads(content) == json.loads(local_path.read_bytes()):
        lines.append(f"allineato: {source} su {ref} (commit {there}) coincide con la copia (commit {here})")
        if commit not in (lock["commit"], "?"):
            if update:
                lock["commit"] = commit
                _write_lock(lock, name, directory)
                lines.append(f"lock aggiornato allo SHA {there} (contenuto invariato)")
            else:
                lines.append("lo SHA registrato non e' l'ultimo commit dello schema: `--update` lo aggiorna")
        return Check(ok, lines)
    ok = False
    lines.append(f"cambiato: {source} su {ref} (commit {there}) differisce dalla copia (commit {here})")
    if update:
        local_path.write_bytes(content)
        lock.update(commit=commit, sha256=sha256_of(local_path))
        _write_lock(lock, name, directory)
        lines.append(f"copia e lock aggiornati in {directory}: rilanciare i test e aprire una PR")
    else:
        lines.append("per riallinearsi: `python -m supervisor contracts check --update`, poi i test e una PR")
    return Check(ok, lines)


def cmd_check(args, settings) -> int:
    from supervisor.collectors.http import RequestsHttp

    result = check(RequestsHttp(), settings.github_token, update=args.update, ref=args.ref)
    for line in result.lines:
        print(line)
    return 0 if result.ok else 1


def register(sub) -> None:
    p = sub.add_parser("contracts", help="contratti con gli altri repository (schema di promo_posts)")
    c = p.add_subparsers(dest="contracts_command", required=True)
    k = c.add_parser("check", help="confronta la copia in tests/contracts con lo schema su main di Promo")
    k.add_argument("--update", action="store_true", help="riscrive in locale copia e lock se sono cambiati")
    k.add_argument("--ref", default="main", help="branch o SHA di Promo da confrontare (default main)")
    k.set_defaults(fn=cmd_check)
