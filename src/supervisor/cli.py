"""Riga di comando: `python -m supervisor <comando>`.

Comandi:
- `observe`   raccoglie i fatti, applica le regole, salva stato e snapshot;
- `report`    scrive il brief dallo stato salvato (e con `--notify` lo manda su Telegram);
- `status`    cursori, ultimo run, lock, notifiche rimaste in sospeso;
- `doctor`    verifica configurazione, credenziali e raggiungibilita' (sempre in sola lettura);
- `replay`    un giro di observe su fixture registrate, senza rete (sviluppo e test di recupero);
- `triage`    proposte di priorita' e prossimo passo per i finding nuovi (AI, entro budget);
- `budget`    spesa, prenotazioni e riconciliazione delle chiamate dall'esito incerto;
- `llm smoke` verifica a pagamento minima dell'accesso a un modello del catalogo.

Con `SUP_ENABLED=false` funzionano solo `doctor`, `replay` e i `--dry-run` (che non scrivono e
non inviano nulla). Codici di uscita: 0 ok (anche con sorgenti incomplete, che il report
dichiara), 1 errore interno, 2 configurazione non valida.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from supervisor.collectors.github import GitHubApi, GitHubCollector
from supervisor.collectors.http import FixtureHttp, HttpClient, RequestsHttp
from supervisor.collectors.promo import FirestorePostReader, PostReader, PromoCollector, StaticPostReader
from supervisor.core.budget import BudgetLedger, load_budget, micros_to_usd, usd_to_micros
from supervisor.core.clock import iso, parse_iso, utcnow
from supervisor.core.config import PROVIDER_KEY_ENV, ConfigError, Settings, Sources, load_sources
from supervisor.core.pipeline import LockBusy, observe
from supervisor.core.policy import load_policy
from supervisor.core.scrub import scrub
from supervisor.engineering.tasks import TaskQueue
from supervisor.llm.catalog import load_catalog, load_routing
from supervisor.llm.client import LLMClient
from supervisor.llm.gateway import LLMBlocked, LLMCallFailed, LLMGateway
from supervisor.reporting.brief import build_brief, render_markdown
from supervisor.reporting.messages import brief_message, findings_message, run_url
from supervisor.reporting.telegram import TelegramNotifier
from supervisor.state import open_store
from supervisor.state.store import DECISIONS, StateStore
from supervisor.workers.triage import run_triage

log = logging.getLogger("supervisor")


def build_collectors(sources: Sources, settings: Settings, http: HttpClient,
                     promo_reader: Optional[PostReader] = None) -> list:
    api = GitHubApi(http, settings.github_token)
    collectors: list = [GitHubCollector(repo, api) for repo in sources.github]
    if promo_reader is None and settings.game_firestore_project:
        promo_reader = FirestorePostReader(settings.game_firestore_project, sources.promo.collection)
    collectors.append(PromoCollector(sources.promo, promo_reader))
    if sources.service.url:
        from supervisor.collectors.service import ServiceCollector

        # Timeout piu' lungo: Cloud Run a freddo puo' metterci 10 secondi.
        service_http = http if not isinstance(http, RequestsHttp) else RequestsHttp(timeout=30)
        collectors.append(ServiceCollector(sources.service, service_http, settings.game_bot_token))
    try:
        from supervisor.collectors.posthog import PostHogCollector
        from supervisor.product.metrics import HogQL, load_product

        product = load_product(settings.config_dir)
        hogql = HogQL(http if settings.posthog_api_key else RequestsHttp(), product, settings.posthog_api_key)
        collectors.append(PostHogCollector(product, hogql if settings.posthog_api_key else None))
    except ConfigError:
        pass  # senza config/product.toml la sorgente semplicemente non esiste
    return collectors


def _now(args) -> datetime:
    return parse_iso(args.now) if getattr(args, "now", None) else utcnow()


def _llm_client(settings: Settings) -> Optional[LLMClient]:
    if not settings.ai_enabled:
        return None
    try:
        from supervisor.llm.litellm_client import LiteLLMClient

        return LiteLLMClient()
    except ImportError:
        log.warning("litellm non installato (requirements-ai.txt): nessuna chiamata AI")
        return None


def build_gateway(settings: Settings, store: StateStore, client: Optional[LLMClient] = None) -> LLMGateway:
    budget = load_budget(settings.config_dir)
    return LLMGateway(
        catalog=load_catalog(settings.config_dir), ledger=BudgetLedger(store, budget.limits), policy=load_policy(),
        client=client if client is not None else _llm_client(settings), ai_enabled=settings.ai_enabled,
        task_limits=budget.tasks,
    )


def _budget_summary(settings: Settings, store: StateStore, now: datetime) -> Optional[dict]:
    try:
        return BudgetLedger(store, load_budget(settings.config_dir).limits).summary(now)
    except Exception as exc:  # il brief deterministico non dipende dal budget
        log.warning("riepilogo budget non disponibile: %s", scrub(exc))
        return None


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
        result = notifier.send(store, "findings", findings_message(outcome.new_findings, run_url()), now)
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
    open_findings = store.open_findings()
    decisions = {f.id: d for f in open_findings if (d := store.get_doc(DECISIONS, f.id))}
    snapshot, events = store.latest_snapshot(), store.events_since(iso(now - timedelta(hours=24)))
    budget, tasks = _budget_summary(settings, store, now), TaskQueue(store).list()
    brief = build_brief(snapshot, open_findings, events, now, decisions=decisions, budget=budget, tasks=tasks)
    markdown = render_markdown(brief)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(markdown, encoding="utf-8")
        print(f"brief scritto in {args.out}")
    else:
        print(markdown)
    if args.notify:
        notifier = TelegramNotifier(RequestsHttp(), settings.telegram_bot_token, settings.admin_chat_id)
        message = brief_message(snapshot, open_findings, events, now, decisions=decisions, budget=budget,
                                tasks=tasks, details=run_url())
        result = notifier.send(store, "brief", message, now, once_per_day=args.once_per_day)
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


class _DryRunClient:
    """In dry-run una chiamata reale per errore fallisce subito, senza rete."""

    def complete(self, request):
        raise RuntimeError("dry-run: nessuna chiamata")


def cmd_triage(args, settings: Settings) -> int:
    if not settings.enabled and not args.dry_run:
        print("SUP_ENABLED=false: triage disabilitato (usare --dry-run)")
        return 0
    store = open_store(settings)
    route = load_routing(settings.config_dir)["triage"]
    gateway = build_gateway(settings, store, client=_DryRunClient() if args.dry_run else None)
    items = run_triage(store, gateway, route.model, route.max_output_tokens, _now(args),
                       limit=args.limit or route.max_items_per_run, dry_run=args.dry_run)
    if not items:
        print("nessun finding da triare")
    for item in items:
        cost = f" (stima {item.estimate_usd:.4f} USD)" if item.status == "dry_run" else ""
        print(f"{item.status}: [{item.finding.severity}] {item.finding.subject}: {item.finding.statement}{cost}")
        if item.decision and item.decision.get("state") == "proposed":
            print(f"  -> {item.decision['priority']}/{item.decision['role']}: {item.decision['rationale_summary']}")
        if item.detail:
            print(f"  {item.detail}")
    return 0


def cmd_budget(args, settings: Settings) -> int:
    store = open_store(settings)
    budget = load_budget(settings.config_dir)
    limits = budget.evaluation if args.namespace == "eval" and budget.evaluation else budget.limits
    ledger = BudgetLedger(store, limits, namespace=args.namespace)
    now = _now(args)
    if args.release_open:
        if not args.reason:
            print("--release-open richiede --reason con l'evidenza (per esempio il run con i rifiuti 402)")
            return 2
        for usage in ledger.open_reservations():
            ledger.release(usage["call_id"], now, f"riconciliata a mano: {args.reason}")
            print(f"rilasciata {usage['call_id']} ({micros_to_usd(usage['reserved_micros']):.4f} USD)")
        return 0
    if args.reconcile:
        if args.release:
            usage = ledger.release(args.reconcile, now, "riconciliata a mano: non addebitata")
        elif args.actual is not None:
            usage = ledger.settle(args.reconcile, usd_to_micros(args.actual), now,
                                  note="riconciliata a mano dalla dashboard del provider")
        else:
            print("indicare --actual USD (costo visto sulla dashboard del provider) oppure --release")
            return 2
        print(f"{usage['call_id']}: {usage['state']}, costo {micros_to_usd(usage.get('actual_micros') or 0):.4f} USD")
        return 0
    s = ledger.summary(now)
    print(f"budget approvato: {s['approved']}")
    print(f"mese {s['month']}: speso {micros_to_usd(s['month_actual']):.4f}, prenotato "
          f"{micros_to_usd(s['month_reserved']):.4f}, tetto {micros_to_usd(s['month_hard']):.2f} USD")
    print(f"oggi {s['day']}: speso {micros_to_usd(s['day_actual']):.4f}, prenotato "
          f"{micros_to_usd(s['day_reserved']):.4f}, tetto {micros_to_usd(s['day_hard']):.2f} USD")
    for usage in ledger.open_reservations():
        print(f"  da riconciliare: {usage['call_id']} {usage['model']} "
              f"{micros_to_usd(usage['reserved_micros']):.4f} USD ({usage['created_at']})")
    return 0


def cmd_llm_smoke(args, settings: Settings) -> int:
    if not settings.enabled:
        print("SUP_ENABLED=false: smoke test disabilitato")
        return 0
    store = open_store(settings)
    route = load_routing(settings.config_dir).get("smoke")
    now = _now(args)
    try:
        result = build_gateway(settings, store).call(
            task_id=f"smoke-{args.model}-{now.strftime('%Y%m%dT%H%M%S')}", task="smoke", model_key=args.model,
            system="Rispondi soltanto con la parola: ok", prompt="ok?",
            max_output_tokens=route.max_output_tokens if route else 16, now=now, allow_unverified=True)
    except (LLMBlocked, LLMCallFailed) as exc:
        print(f"smoke {args.model}: KO - {exc}")
        return 1
    r = result.response
    print(f"smoke {args.model}: OK - modello {r.model}, token {r.input_tokens}/{r.output_tokens}, "
          f"costo {micros_to_usd(result.cost_micros):.6f} USD, risposta {r.text[:40]!r}")
    print("Se il modello restituito e' quello atteso, impostare a mano access_verified = true in config/models.toml.")
    return 0


def _ai_checks(settings: Settings) -> list[tuple[str, bool, str]]:
    """Controlli AI: informativi finche' SUP_AI_ENABLED=false, bloccanti quando e' attivo."""
    strict = settings.ai_enabled
    try:
        catalog = load_catalog(settings.config_dir)
        routing = load_routing(settings.config_dir)
        budget = load_budget(settings.config_dir)
        policy = load_policy()
    except ConfigError as exc:
        return [("ai config", False, str(exc))]
    checks: list[tuple[str, bool, str]] = [
        ("ai interruttore", True, f"SUP_AI_ENABLED={settings.ai_enabled}"),
        ("ai policy", True, f"v{policy.version} call_paid_llm={policy.decide('call_paid_llm')}"),
    ]
    limits = budget.limits
    checks.append(("ai budget", limits.approved or not strict,
                   f"approvato da {limits.approved_by} il {limits.approved_on}" if limits.approved
                   else "non approvato: nessuna chiamata a pagamento"))
    for task, route in routing.items():
        if not route.model:
            continue
        model = catalog.get(route.model)
        if model is None:
            checks.append((f"ai {task}", False, f"modello {route.model} assente dal catalogo"))
            continue
        ready = model.enabled and model.access_verified
        checks.append((f"ai {task}", ready or not strict,
                       f"{model.key}: " + ("pronto" if ready else "accesso non verificato o disabilitato")))
        key_name = PROVIDER_KEY_ENV.get(model.provider, "")
        has_key = bool(key_name and os.environ.get(key_name))
        checks.append((f"ai chiave {model.provider}", has_key or not strict,
                       f"{key_name} {'presente' if has_key else 'assente'}"))
    if strict:
        try:
            import litellm  # noqa: F401

            checks.append(("ai libreria", True, "litellm installato"))
        except ImportError:
            checks.append(("ai libreria", False, "pip install -r requirements-ai.txt"))
    return checks


def cmd_watchdog(args, settings: Settings) -> int:
    """Controlli sul supervisore stesso; avvisa su Telegram al massimo una volta al giorno per problema."""
    from supervisor.core import watchdog

    if not settings.enabled and not args.test:
        print("SUP_ENABLED=false: watchdog inattivo")
        return 0
    store = open_store(settings)
    now = _now(args)
    budget = load_budget(settings.config_dir)
    ledgers = [BudgetLedger(store, budget.limits)]
    if budget.evaluation:
        ledgers.append(BudgetLedger(store, budget.evaluation, namespace="eval"))
    problems = watchdog.check(store, now, args.max_age_hours, tuple(ledgers))
    for problem in problems:
        print(f"PROBLEMA {problem.key}: {problem.text}")
    if not problems:
        print("watchdog: tutto regolare")
    text = watchdog.message(problems, run_url(), test=args.test)
    if text:
        result = TelegramNotifier(RequestsHttp(), settings.telegram_bot_token, settings.admin_chat_id).send(
            store, "watchdog", text, now)
        print(f"notifica watchdog: {result.status} {result.detail}".rstrip())
    return 0


def cmd_prune(args, settings: Settings) -> int:
    if not settings.enabled:
        print("SUP_ENABLED=false: pulizia inattiva")
        return 0
    removed = open_store(settings).prune_expired(_now(args), limit=args.limit)
    total = sum(removed.values())
    print(f"documenti scaduti cancellati: {total} " + ", ".join(f"{k} {v}" for k, v in removed.items() if v))
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
    checks.extend(_ai_checks(settings))
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
    p.add_argument("--once-per-day", action="store_true",
                   help="al massimo un brief al giorno, anche se il contenuto cambia (giri pianificati)")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("triage", help="proposte AI per i finding senza decisione")
    p.add_argument("--dry-run", action="store_true", help="stima costi e controlli, nessuna chiamata")
    p.add_argument("--limit", type=int, help="massimo di finding in questo giro")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_triage)

    p = sub.add_parser("budget", help="spesa AI e riconciliazione")
    p.add_argument("--reconcile", metavar="CALL_ID", help="chiude una chiamata dall'esito incerto")
    p.add_argument("--namespace", default="", choices=("", "eval"), help="budget operativo (default) o evaluation")
    p.add_argument("--release-open", action="store_true",
                   help="rilascia tutte le prenotazioni aperte del namespace (solo se verificate come non addebitate)")
    p.add_argument("--reason", help="evidenza della riconciliazione")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--actual", type=float, help="costo reale in USD visto sulla dashboard del provider")
    group.add_argument("--release", action="store_true", help="la chiamata non e' stata addebitata")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_budget)

    p = sub.add_parser("llm", help="strumenti per i modelli")
    llm_sub = p.add_subparsers(dest="llm_command", required=True)
    s = llm_sub.add_parser("smoke", help="chiamata minima a pagamento per verificare l'accesso a un modello")
    s.add_argument("model", help="chiave del catalogo, es. gpt-6-luna")
    s.add_argument("--now", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_llm_smoke)

    sub.add_parser("status", help="stato interno").set_defaults(fn=cmd_status)

    p = sub.add_parser("prune", help="cancella i documenti operativi oltre la conservazione (M6)")
    p.add_argument("--limit", type=int, default=500, help="massimo per collezione e per giro")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("watchdog", help="controlla che il supervisore giri e sia coerente (M6)")
    p.add_argument("--max-age-hours", type=float, default=4.0, help="eta' massima dell'ultimo giro di observe")
    p.add_argument("--test", action="store_true", help="manda un messaggio anche se va tutto bene")
    p.add_argument("--now", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_watchdog)

    from supervisor.cli_engineer import register
    from supervisor.cli_eval import register as register_eval
    from supervisor.cli_growth import register as register_growth

    register(sub)
    register_eval(sub)
    register_growth(sub)
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
