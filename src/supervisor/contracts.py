"""Contratti con gli altri repository: `python -m supervisor contracts check` e `contracts notify`.

I contratti sono gli schemi pubblicati da Promo Studio per i documenti che il collector Promo legge nel
Firestore del gioco:
- `promo_post.v1`: la coda `promo_posts` (`docs/schemas/promo_post.v1.json`, issue #11);
- `promo_brief_decision.v1`: le decisioni sui brief del supervisore, `promo_brief_decisions`
  (`docs/schemas/promo_brief_decision.v1.json`, issue #14).
Il supervisore ne tiene una copia in `tests/contracts/` insieme a un file `.lock.json` con il repository, il
percorso, lo SHA del commit di Promo da cui viene e lo sha256 del file. I test validano le fixture contro la
copia, senza rete.

`contracts check` confronta ogni copia con lo schema su `main` di Promo (API GitHub, sola lettura, repo
pubblico: il token e' facoltativo e serve solo per il limite di richieste) e dice se e' cambiato o se e'
comparsa una versione nuova (`promo_post.v2.json`). Non modifica nulla, salvo `--update`, che riscrive
copia e lock in locale (anche per un contratto nuovo, di cui c'e' solo il lock): il cambio va poi riletto,
fatto passare dai test e proposto in una PR. `--report` salva l'esito in JSON per `contracts notify`.

`contracts notify --report` avvisa Michele su Telegram per ogni contratto cambiato o con una versione nuova,
una sola volta per cambiamento: la chiave dipende dal contenuto su Promo e dalla copia, si prenota nello
stato prima dell'invio (`TelegramNotifier`) e, a invio riuscito, resta in `contract_alerts`, che non scade.
"Non verificabile" (rete, limite di richieste, 404) non e' un cambiamento: resta nel riepilogo della run.

Codici di uscita di `check`: 0 allineato, 1 cambiato o non verificabile.
`notify`: 1 solo se un invio fallisce.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from supervisor.collectors.http import HttpClient
from supervisor.core.clock import iso
from supervisor.core.models import stable_hash
from supervisor.core.scrub import scrub
from supervisor.reporting.messages import esc, link

API = "https://api.github.com"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "tests" / "contracts"
PROMO_POST = "promo_post.v1"
BRIEF_DECISION = "promo_brief_decision.v1"
CONTRACTS = (PROMO_POST, BRIEF_DECISION)
# Dove il collector elenca i campi che legge, per ogni contratto: da ricontrollare dopo un cambio.
READ_FIELDS = {PROMO_POST: "POST_FIELDS e HISTORY_FIELDS", BRIEF_DECISION: "DECISION_FIELDS"}
VERSIONED = re.compile(r"^(?P<name>[a-z_]+)\.v(?P<version>\d+)\.json$")
# Avvisi di contratto gia' mandati: non scadono, cosi' un cambio non riallineato non si ripete ogni mese
# quando `notifications` viene potata.
CONTRACT_ALERTS = "contract_alerts"
ALIGNED, CHANGED, UNVERIFIABLE = "aligned", "changed", "unverifiable"


@dataclass
class Check:
    ok: bool
    lines: list[str]
    name: str = PROMO_POST
    state: str = ALIGNED  # aligned | changed | unverifiable
    source: str = ""  # <repo>/<percorso>
    ref: str = "main"
    remote_commit: str = ""
    local_commit: str = ""
    remote_sha256: str = ""
    local_sha256: str = ""
    newer: list[str] = field(default_factory=list)

    @property
    def drift(self) -> bool:
        """Qualcosa da riallineare: schema cambiato o versione nuova su Promo (non "non verificabile")."""
        return self.state == CHANGED or bool(self.newer)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Check:
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in known})


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
    here = lock.get("commit") or ""
    result = Check(True, [], name=name, source=source, ref=ref, local_commit=here,
                   local_sha256=lock.get("sha256") or "")
    try:
        content, commit, names = fetch_remote(http, token, lock, ref)
    except Exception as exc:  # rete, limite di richieste, file rimosso
        error = scrub(f"{type(exc).__name__}: {exc}")[:300]
        result.ok, result.state = False, UNVERIFIABLE
        result.lines.append(f"non verificabile: {source} su {ref} ({error})")
        return result
    result.remote_commit, result.remote_sha256 = commit, hashlib.sha256(content).hexdigest()
    result.newer = newer_versions(name, names)
    if result.newer:
        result.ok = False
        result.lines.append(f"versione nuova in Promo: {', '.join(result.newer)} "
                            "(serve un'issue e un riallineamento del collector)")
    short_here, short_there = here[:12] or "nessuna", commit[:12]
    if local_path.exists() and json.loads(content) == json.loads(local_path.read_bytes()):
        result.lines.append(f"allineato: {source} su {ref} (commit {short_there}) coincide con la copia "
                            f"(commit {short_here})")
        if commit not in (here, "?"):
            if update:
                lock["commit"] = commit
                _write_lock(lock, name, directory)
                result.lines.append(f"lock aggiornato allo SHA {short_there} (contenuto invariato)")
            else:
                result.lines.append("lo SHA registrato non e' l'ultimo commit dello schema: "
                                    "`--update` lo aggiorna")
        return result
    result.ok, result.state = False, CHANGED
    what = "differisce dalla copia" if local_path.exists() else "non ha ancora una copia"
    result.lines.append(f"cambiato: {source} su {ref} (commit {short_there}) {what} (commit {short_here})")
    if update:
        local_path.write_bytes(content)
        lock.update(commit=commit, sha256=sha256_of(local_path))
        _write_lock(lock, name, directory)
        result.lines.append(f"copia e lock aggiornati in {directory}: rilanciare i test e aprire una PR")
    else:
        result.lines.append("per riallinearsi: `python -m supervisor contracts check --update`, "
                            "poi i test e una PR")
    return result


# --- avviso su Telegram -------------------------------------------------------------------------------

def alert_key(result: Check) -> str:
    """Stabile per lo stesso cambiamento: contenuto su Promo, copia registrata e versioni nuove."""
    basis = f"{result.remote_sha256}|{result.local_sha256}|{','.join(sorted(result.newer))}"
    return f"contract-{result.name}-{stable_hash(basis)[:16]}"


def _commit_link(result: Check, sha: str) -> str:
    if not sha or sha == "?":
        return "<code>?</code>"
    repo = result.source.split("/", 2)
    url = f"https://github.com/{repo[0]}/{repo[1]}/commit/{sha}" if len(repo) == 3 else ""
    return f"<code>{esc(sha[:12])}</code>" + (f" ({link(url, 'apri')})" if url else "")


def alert_message(result: Check, details: str = "") -> str:
    path = result.source.split("/", 2)[-1]
    lines = [f"⚠️ <b>Contratto con Promo da riallineare</b> · <code>{esc(result.name)}</code>"]
    if result.state == CHANGED:
        lines.append(f"Lo schema <code>{esc(path)}</code> su {esc(result.ref)} di Promo non coincide più "
                     "con la copia del supervisore.")
    if result.newer:
        lines.append(f"Versione nuova in Promo: <code>{esc(', '.join(result.newer))}</code>.")
    lines += [f"SHA su Promo: {_commit_link(result, result.remote_commit)}",
              f"SHA della copia: {_commit_link(result, result.local_commit)}",
              "",
              "<b>Cosa fare</b>",
              "1. Su un branch: <code>python -m supervisor contracts check --update</code>",
              f"2. <code>python -m pytest -q</code> e verifica che il collector Promo legga ancora i campi "
              f"giusti (<code>{esc(READ_FIELDS.get(result.name, 'collectors/promo.py'))}</code>)",
              "3. PR con la copia nuova (e il collector adeguato, se serve): il merge lo decidi tu"]
    if result.newer:
        lines.append("Per una versione nuova serve anche un'issue: la copia attuale resta valida finché "
                     "Promo scrive la versione vecchia.")
    lines.append("<i>Avviso unico per questo cambiamento: non si ripete ai prossimi controlli.</i>")
    if details:
        lines.append(link(details, "📄 Run del controllo"))
    return "\n".join(lines)


def notify(results: list[Check], store, notifier, now: datetime, details: str = "") -> tuple[bool, list[str]]:
    """Un messaggio per ogni contratto da riallineare, una volta sola per cambiamento. (tutto ok, righe)."""
    ok, lines = True, []
    for result in results:
        if result.state == UNVERIFIABLE:
            lines.append(f"{result.name}: non verificabile, nessun avviso su Telegram (solo nel riepilogo)")
            continue
        if not result.drift:
            continue
        key = alert_key(result)
        if store.get_doc(CONTRACT_ALERTS, key):
            lines.append(f"{result.name}: cambiamento gia' segnalato ({key})")
            continue
        outcome = notifier.send(store, "contract", alert_message(result, details), now, key=key)
        if outcome.status == "sent":
            store.put_doc(CONTRACT_ALERTS, key, {
                "key": key, "name": result.name, "source": result.source, "state": result.state,
                "remote_commit": result.remote_commit, "local_commit": result.local_commit,
                "newer": result.newer, "sent_at": iso(now)}, iso(now))
            lines.append(f"{result.name}: avviso mandato su Telegram ({key})")
        elif outcome.status == "duplicate":
            lines.append(f"{result.name}: avviso gia' prenotato o in sospeso ({key}), non ripetuto")
        else:
            ok = ok and outcome.status == "not_configured"
            lines.append(f"{result.name}: avviso non mandato ({outcome.status}: {outcome.detail})")
    return ok, lines or ["contratti allineati: nessun avviso"]


# --- riga di comando ------------------------------------------------------------------------------

def write_report(results: list[Check], path: Path, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"checked_at": iso(now), "results": [r.to_dict() for r in results]}
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def read_report(path: Path) -> list[Check]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [Check.from_dict(r) for r in doc.get("results") or []]


def cmd_check(args, settings) -> int:
    from supervisor.collectors.http import RequestsHttp
    from supervisor.core.clock import utcnow

    http = RequestsHttp()
    results = [check(http, settings.github_token, name=name, update=args.update, ref=args.ref)
               for name in CONTRACTS]
    for result in results:
        for line in result.lines:
            print(f"{result.name}: {line}")
    if args.report:
        write_report(results, Path(args.report), utcnow())
    return 0 if all(r.ok for r in results) else 1


def cmd_notify(args, settings) -> int:
    from supervisor.collectors.http import RequestsHttp
    from supervisor.core.clock import utcnow
    from supervisor.reporting.messages import run_url
    from supervisor.reporting.telegram import TelegramNotifier
    from supervisor.state import open_store

    path = Path(args.report)
    if not path.exists():
        print(f"nessun esito di `contracts check` in {path}: niente da segnalare")
        return 0
    if not settings.enabled:
        print("SUP_ENABLED=false: nessun avviso sui contratti")
        return 0
    notifier = TelegramNotifier(RequestsHttp(), settings.telegram_bot_token, settings.admin_chat_id)
    ok, lines = notify(read_report(path), open_store(settings), notifier, utcnow(), run_url())
    for line in lines:
        print(line)
    return 0 if ok else 1


def register(sub) -> None:
    p = sub.add_parser("contracts", help="contratti con gli altri repository (schemi di Promo)")
    c = p.add_subparsers(dest="contracts_command", required=True)
    k = c.add_parser("check", help="confronta le copie in tests/contracts con gli schemi su main di Promo")
    k.add_argument("--update", action="store_true", help="riscrive in locale copia e lock se sono cambiati")
    k.add_argument("--ref", default="main", help="branch o SHA di Promo da confrontare (default main)")
    k.add_argument("--report", help="salva l'esito in JSON (per `contracts notify`)")
    k.set_defaults(fn=cmd_check)
    n = c.add_parser("notify", help="avvisa su Telegram dei contratti da riallineare, una volta sola")
    n.add_argument("--report", required=True, help="esito salvato da `contracts check --report`")
    n.set_defaults(fn=cmd_notify)
