"""Riga di comando: `python -m supervisor <comando>`.

Comandi:
- `observe`   raccoglie i fatti, applica le regole, salva stato e snapshot;
- `report`    scrive il brief dallo stato salvato (e con `--notify` lo manda su Telegram);
- `status`    cursori, ultimo run, lock, notifiche rimaste in sospeso;
- `doctor`    verifica configurazione, credenziali e raggiungibilita' (sempre in sola lettura);
- `replay`    un giro di observe su fixture registrate, senza rete (sviluppo e test di recupero).

Con `SUP_ENABLED=false` funzionano solo `doctor`, `replay` e i `--dry-run` (che non scrivono e
non inviano nulla). Codici di uscita: 0 ok (anche con sorgenti incomplete, che il report
dichiara), 1 errore interno, 2 configurazione non valida.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from supervisor.collectors.github import GitHubApi, GitHubCollector
from supervisor.collectors.http import FixtureHttp, HttpClient, RequestsHttp
from supervisor.collectors.promo import FirestorePostReader, PostReader, PromoCollector, StaticPostReader
from supervisor.core.clock import iso, parse_iso, utcnow
from supervisor.core.config import ConfigError, Settings, Sources, load_sources
from supervisor.core.pipeline import LockBusy, observe
from supervisor.core.scrub import scrub
from supervisor.reporting.brief import build_brief, render_findings_alert, render_markdown, render_telegram
from supervisor.reporting.telegram import TelegramNotifier
from supervisor.state import open_store
from supervisor.state.store import StateStore

log = logging.getLogger("supervisor")


def build_collectors(sources: Sources, settings: Settings, http: HttpClient,
                     promo_reader: Optional[PostReader] = None) -> list:
    api = GitHubApi(http, settings.github_token)
    collectors: list = [GitHubCollector(repo, api) for repo in sources.github]
    if promo_reader is None and settings.game_firestore_project:
        promo_reader = FirestorePostReader(settings.game_firestore_project, sources.promo.collection)
    collectors.append(PromoCollector(sources.promo, promo_reader))
    return collectors


def _now(args) -> datetime:
    return parse_iso(args.now) if getattr(args, "now", None) else utcnow()


def _print_outcome(outcome) -> None:
    run = outcome.run
    prefix = "[dry-run] " if run.dry_run else ""
    print(f"{prefix}run {run.id}: {run.status}, eventi nuovi {run.new_events}, "
          f"finding nuovi {len(outcome.new_findings)}, risolti {len(outcome.resolved)}")
    for source, completeness in sorted(run.sources.items()):
        print(f"  {source}: {completeness}")
    for finding in outcome.new_findings:
        print(f"  + [{finding.severity}] {finding.subject}: {finding.statement}")


def _run_observe(args, settings: Settings, store: StateStore, http: HttpClient,
                 promo_reader: Optional[PostReader], notify: bool) -> int:
    sources = load_sources(settings.config_dir)
    now = _now(args)
    try:
        outcome = observe(store, build_collectors(sources, settings, http, promo_reader), sources, now,
                          dry_run=args.dry_run)
    except LockBusy as exc:
        print(f"saltato: {exc}")
        return 0
    _print_outcome(outcome)
    if notify and outcome.new_findings and not args.dry_run:
        notifier = TelegramNotifier(RequestsHttp(), settings.telegram_bot_token, settings.admin_chat_id)
        result = notifier.send(store, "findings", render_findings_alert(outcome.new_findings), now)
        print(f"notifica finding: {result.status} {result.detail}".rstrip())
    return 0


def cmd_observe(args, settings: Settings) -> int:
    if not settings.enabled and not args.dry_run:
        print("SUP_ENABLED=false: observe disabilitato (usare --dry-run, replay o doctor)")
        return 0
    return _run_observe(args, settings, open_store(settings), RequestsHttp(), None, args.notify_findings)


def cmd_replay(args, settings: Settings) -> int:
    posts = []
    if args.promo_fixture:
        import json

        posts = json.loads(Path(args.promo_fixture).read_text(encoding="utf-8"))
    reader = StaticPostReader(posts) if args.promo_fixture else None
    return _run_observe(args, settings, open_store(settings), FixtureHttp.from_file(args.fixtures), reader, False)


def cmd_report(args, settings: Settings) -> int:
    if not settings.enabled and args.notify:
        print("SUP_ENABLED=false: il brief si scrive ma non si invia")
        args.notify = False
    store = open_store(settings)
    now = _now(args)
    brief = build_brief(store.latest_snapshot(), store.open_findings(),
                        store.events_since(iso(now - timedelta(hours=24))), now)
    markdown = render_markdown(brief)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"brief scritto in {args.out}")
    else:
        print(markdown)
    if args.notify:
        notifier = TelegramNotifier(RequestsHttp(), settings.telegram_bot_token, settings.admin_chat_id)
        result = notifier.send(store, "brief", render_telegram(brief), now)
        print(f"notifica brief: {result.status} {result.detail}".rstrip())
        return 1 if result.status == "failed" else 0
    return 0


def cmd_status(args, settings: Settings) -> int:
    store = open_store(settings)
    run = store.last_run()
    print(f"SUP_ENABLED={settings.enabled} store={settings.store}")
    print(f"ultimo run: {run.id} {run.status} ({run.finished_at})" if run else "ultimo run: nessuno")
    if run:
        for source, completeness in sorted(run.sources.items()):
            print(f"  {source}: {completeness}")
    lock = store.get_lock("observe")
    print(f"lock observe: {lock['owner']} fino a {lock['lease_until']}" if lock else "lock observe: libero")
    print("cursori:")
    for stream, doc in sorted(store.cursors().items()):
        cursor = {k: v for k, v in (doc.get("cursor") or {}).items() if k != "summary"}
        print(f"  {stream}: {cursor}")
    findings = store.open_findings()
    print(f"finding aperti: {len(findings)}")
    pending = store.pending_notifications()
    if pending:
        print("notifiche rimaste in sospeso (possibile invio interrotto, verificare su Telegram):")
        for doc in pending:
            print(f"  {doc['key']} ({doc.get('claimed_at')})")
    return 0


def cmd_doctor(args, settings: Settings) -> int:
    checks: list[tuple[str, bool, str]] = []
    try:
        sources = load_sources(settings.config_dir)
        checks.append(("config", True, f"{len(sources.github)} repository"))
    except ConfigError as exc:
        checks.append(("config", False, str(exc)))
        sources = None
    checks.append(("interruttore", True, f"SUP_ENABLED={settings.enabled}"))
    try:
        store = open_store(settings)
        store.last_run()
        checks.append(("stato", True, settings.store))
    except Exception as exc:
        checks.append(("stato", False, scrub(f"{settings.store}: {type(exc).__name__}: {exc}")[:200]))
    http = RequestsHttp()
    api = GitHubApi(http, settings.github_token)
    if not settings.github_token:
        checks.append(("github token", False, "SUP_GITHUB_TOKEN mancante (limite anonimo: 60 richieste/ora)"))
    for repo in sources.github if sources else ():
        try:
            meta = api.get(f"/repos/{repo.repo}").body
            checks.append((f"github {repo.repo}", True, f"default {meta.get('default_branch')}"))
        except Exception as exc:
            checks.append((f"github {repo.repo}", False, scrub(str(exc))[:200]))
    if settings.game_firestore_project and sources:
        try:
            posts = FirestorePostReader(settings.game_firestore_project, sources.promo.collection).list_posts()
            checks.append(("promo_posts", True, f"{len(posts)} post leggibili"))
        except Exception as exc:
            checks.append(("promo_posts", False, scrub(f"{type(exc).__name__}: {exc}")[:200]))
    else:
        checks.append(("promo_posts", True, "non configurato (SUP_GAME_FIRESTORE_PROJECT vuoto)"))
    ok, detail = TelegramNotifier(http, settings.telegram_bot_token, settings.admin_chat_id).check()
    checks.append(("telegram", ok or not settings.telegram_bot_token,
                   detail if settings.telegram_bot_token else "non configurato: nessuna notifica"))
    for name, passed, detail in checks:
        print(f"{'OK ' if passed else 'KO '} {name}: {detail}")
    return 0 if all(passed for _, passed, _ in checks) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="supervisor", description="GTP Supervisor (M1: sola lettura)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("observe", help="raccoglie fatti e applica le regole")
    p.add_argument("--dry-run", action="store_true", help="non scrive stato e non invia nulla")
    p.add_argument("--notify-findings", action="store_true", help="avvisa su Telegram se ci sono finding nuovi")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_observe)

    p = sub.add_parser("replay", help="observe su fixture registrate, senza rete")
    p.add_argument("--fixtures", required=True, help="file JSON di risposte GitHub")
    p.add_argument("--promo-fixture", help="file JSON con l'elenco dei post promo")
    p.add_argument("--now", help="istante simulato, ISO UTC")
    p.set_defaults(fn=cmd_replay, dry_run=False)

    p = sub.add_parser("report", help="scrive il brief dallo stato salvato")
    p.add_argument("--out", help="file Markdown di destinazione")
    p.add_argument("--notify", action="store_true", help="invia il brief su Telegram")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_report)

    sub.add_parser("status", help="stato interno").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="verifica configurazione e credenziali").set_defaults(fn=cmd_doctor)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        return args.fn(args, settings)
    except ConfigError as exc:
        print(f"configurazione non valida: {scrub(exc)}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"errore interno: {scrub(f'{type(exc).__name__}: {exc}')}", file=sys.stderr)
        return 1
