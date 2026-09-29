"""Comandi `python -m supervisor growth ...` (M4).

- `growth review`  review settimanale: metriche PostHog, una proposta del modello con fattibilita' calcolata
                   dal codice e, se serve, una bozza di brief per Promo Studio. Una sola proposta a settimana
                   (idempotente); `--dry-run` stima il costo; `--notify` manda il riassunto su Telegram.
- `growth briefs`  brief pubblicati per Promo (`promo_briefs`, status proposed) ed export in JSON.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from supervisor.cli import _now, build_gateway
from supervisor.collectors.http import RequestsHttp
from supervisor.core.config import Settings
from supervisor.llm.catalog import load_routing
from supervisor.product.growth import (
    BRIEF_STATUS_PROPOSED,
    PROMO_BRIEFS,
    legacy_or_payload,
    load_promo_facts,
    promo_import_payload,
    review_markdown,
    weekly_review,
)
from supervisor.product.metrics import HogQL, collect_product, load_product
from supervisor.reporting.messages import growth_message, run_url
from supervisor.reporting.telegram import TelegramNotifier
from supervisor.state import open_store


def _facts(settings: Settings, store, product) -> dict:
    if settings.posthog_api_key:
        from dataclasses import asdict

        return asdict(collect_product(HogQL(RequestsHttp(), product, settings.posthog_api_key), product))
    snapshot = store.latest_snapshot() or {}
    for source in snapshot.get("sources", []):
        if source.get("kind") == "posthog" and source.get("facts"):
            return source["facts"]
    return {}


def cmd_review(args, settings: Settings) -> int:
    if not settings.enabled and not args.dry_run:
        print("SUP_ENABLED=false: review disabilitata (usare --dry-run)")
        return 0
    store = open_store(settings)
    product = load_product(settings.config_dir)
    route = load_routing(settings.config_dir)["growth_weekly"]
    now = _now(args)
    facts = _facts(settings, store, product)
    result = weekly_review(store, build_gateway(settings, store), facts, product, load_promo_facts(settings.config_dir),
                           route.model, route.max_output_tokens, now, dry_run=args.dry_run, force=args.force)
    text = review_markdown(result)
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / "review.md").write_text(text, encoding="utf-8")
        if result.brief:
            _export(result.brief, Path(args.out) / "briefs")
    print(text)
    if args.dry_run:
        print(f"[dry-run] stima {result.estimate_usd:.4f} USD — {result.detail}")
    for warning in result.warnings:
        print(f"ATTENZIONE: {warning}")
    if args.notify and result.status == "proposed" and result.proposal:
        message = growth_message(result.week, result.proposal, result.brief, run_url())
        TelegramNotifier(RequestsHttp(), settings.telegram_bot_token, settings.admin_chat_id).send(
            store, "growth", message, now)
    return 0


def _export(brief: dict, folder: Path) -> Path:
    import json

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{brief['campaign_id']}.json"
    path.write_text(json.dumps(promo_import_payload(brief), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def cmd_briefs(args, settings: Settings) -> int:
    store = open_store(settings)
    # `state: draft` e' il formato di prima dell'issue #9 (documento piatto, mai letto da Promo).
    docs = store.query_docs(PROMO_BRIEFS, "status", BRIEF_STATUS_PROPOSED) + store.query_docs(
        PROMO_BRIEFS, "state", "draft")
    for doc in sorted(docs, key=lambda b: b["created_at"]):
        brief = legacy_or_payload(doc)
        where = f"per Promo fino al {doc['expires_at'][:10]}" if doc.get("expires_at") else "solo file (formato vecchio)"
        print(f"{brief['campaign_id']}: {brief['channel']} / {brief['language']} / {brief['format']} — {brief['cta']}"
              f" [{where}]")
        if args.export:
            print(f"  -> {_export(brief, Path(args.export))} (python -m promo brief-import <file>)")
    if not docs:
        print("nessuna bozza di brief")
    return 0


def register(sub) -> None:
    p = sub.add_parser("growth", help="review product/growth settimanale e brief per Promo (M4)")
    g = p.add_subparsers(dest="growth_command", required=True)
    r = g.add_parser("review", help="metriche, una proposta motivata e l'eventuale brief per Promo")
    r.add_argument("--dry-run", action="store_true", help="nessuna chiamata AI: stima del costo")
    r.add_argument("--force", action="store_true", help="rifà la proposta anche se esiste per questa settimana")
    r.add_argument("--notify", action="store_true", help="riassunto su Telegram")
    r.add_argument("--out", help="cartella per review.md")
    r.add_argument("--now", help=argparse.SUPPRESS)
    r.set_defaults(fn=cmd_review)
    b = g.add_parser("briefs", help="bozze di brief per Promo Studio")
    b.add_argument("--export", help="cartella dove scrivere i JSON per `python -m promo brief-import`")
    b.set_defaults(fn=cmd_briefs)
