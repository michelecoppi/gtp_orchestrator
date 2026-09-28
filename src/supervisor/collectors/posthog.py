"""Collector PostHog (M4): metriche di prodotto in sola lettura, con cache nel cursore.

Le metriche cambiano lentamente e le query sono piu' pesanti di quelle GitHub: si interrogano al massimo
ogni `refresh_hours`; nei giri intermedi i fatti si prendono dal cursore, dichiarando da quando risalgono.
Senza chiave la sorgente e' "non configurata", non vuota.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Optional

from supervisor.core.clock import hours_between, iso
from supervisor.core.models import CollectResult, SourceReport, StreamResult
from supervisor.product.metrics import HogQL, ProductConfig, collect_product


class PostHogCollector:
    kind = "posthog"

    def __init__(self, config: ProductConfig, hogql: Optional[HogQL]) -> None:
        self.config = config
        self.hogql = hogql
        self.source = f"posthog:{config.project_id}"

    def collect(self, cursors: dict[str, Optional[dict]], now: datetime) -> CollectResult:
        stream = f"{self.source}#metrics"
        if self.hogql is None or not self.hogql.api_key:
            return CollectResult([], SourceReport(self.source, self.kind, ok=False, configured=False))
        cached = cursors.get(stream) or {}
        if cached.get("facts") and hours_between(cached["collected_at"], now) < self.config.refresh_hours:
            facts = {**cached["facts"], "collected_at": cached["collected_at"], "cached": True}
            return CollectResult([StreamResult(stream, [], cached)],
                                 SourceReport(self.source, self.kind, ok=not facts.get("errors"), facts=facts,
                                              errors=list(facts.get("errors") or [])))
        product = collect_product(self.hogql, self.config)
        facts = {**asdict(product), "collected_at": iso(now), "cached": False}
        ok = not product.errors
        # Con errori il cursore non si aggiorna: al giro successivo si riprova invece di tenere dati parziali.
        cursor = {"collected_at": iso(now), "facts": asdict(product)} if ok else None
        streams = [StreamResult(stream, [], cursor)] if ok else [StreamResult(stream, ok=False,
                                                                              error="; ".join(product.errors))]
        return CollectResult(streams, SourceReport(self.source, self.kind, ok=ok, facts=facts,
                                                   errors=list(product.errors)))
