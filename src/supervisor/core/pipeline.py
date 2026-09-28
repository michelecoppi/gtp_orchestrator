"""Il giro di osservazione: raccogli, registra, applica le regole, salva lo snapshot.

L'ordine protegge dai guasti:
1. lock con lease (un solo giro alla volta, anche se `concurrency` di Actions non bastasse);
2. per ogni flusso riuscito, eventi e cursore insieme; un flusso fallito non tocca il cursore;
3. regole sugli eventi davvero nuovi e sui fatti attuali;
4. finding e risoluzioni, snapshot, record del run.
Se il processo muore a meta', il giro successivo rilegge i fatti non confermati e la
deduplicazione impedisce doppioni. In dry-run non si scrive nulla: si calcola solo cosa
cambierebbe, leggendo lo stato attuale.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from supervisor.collectors.base import Collector
from supervisor.core.clock import iso
from supervisor.core.config import Sources
from supervisor.core.models import Event, Finding, Run, SourceReport
from supervisor.core.scrub import scrub
from supervisor.rules.engine import evaluate
from supervisor.state.store import StateStore

LOCK_NAME = "observe"
LOCK_TTL_SECONDS = 15 * 60


class LockBusy(RuntimeError):
    pass


@dataclass
class ObserveOutcome:
    run: Run
    reports: list[SourceReport] = field(default_factory=list)
    new_events: list[Event] = field(default_factory=list)
    new_findings: list[Finding] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)


def observe(store: StateStore, collectors: Sequence[Collector], sources: Sources, now: datetime,
            dry_run: bool = False, owner: Optional[str] = None) -> ObserveOutcome:
    run = Run(id=f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}", started_at=iso(now), dry_run=dry_run)
    owner = owner or run.id
    if not dry_run and not store.acquire_lock(LOCK_NAME, owner, LOCK_TTL_SECONDS, now):
        raise LockBusy("un altro giro di observe e' in corso")
    try:
        outcome = _observe(store, collectors, sources, now, run, dry_run)
    except Exception as exc:
        run.status, run.error, run.finished_at = "failed", scrub(f"{type(exc).__name__}: {exc}")[:500], iso(now)
        if not dry_run:
            store.save_run(run)
        raise
    finally:
        if not dry_run:
            store.release_lock(LOCK_NAME, owner)
    return outcome


def _observe(store: StateStore, collectors: Sequence[Collector], sources: Sources, now: datetime,
             run: Run, dry_run: bool) -> ObserveOutcome:
    reports: list[SourceReport] = []
    new_events: list[Event] = []
    for collector in collectors:
        cursors = {name: doc["cursor"] for name, doc in store.cursors().items() if name.startswith(collector.source + "#")}
        result = collector.collect(cursors, now)
        reports.append(result.report)
        for stream in result.streams:
            if not stream.ok:
                continue
            if dry_run:
                known = store.existing_events([e.dedupe_key for e in stream.events])
                new_events.extend(_unique(e for e in stream.events if e.dedupe_key not in known))
            else:
                new_events.extend(store.commit_stream(stream.stream, stream.events, stream.cursor))

    rules = evaluate(new_events, reports, store.open_findings(), sources, now, run.id)
    if dry_run:
        known_findings = store.existing_findings([f.id for f in rules.findings])
        new_findings = _unique_findings(f for f in rules.findings if f.id not in known_findings)
        resolved = rules.resolve
    else:
        new_findings = store.add_findings(_unique_findings(rules.findings))
        resolved = store.resolve_findings(rules.resolve, iso(now))
        store.save_snapshot(run.id, iso(now), reports)

    run.status = "completed" if all(r.ok or not r.configured for r in reports) else "partial"
    run.finished_at = iso(now)
    run.new_events = len(new_events)
    run.new_findings = [f.id for f in new_findings]
    run.resolved_findings = resolved
    run.sources = {r.source: r.completeness for r in reports}
    if not dry_run:
        store.save_run(run)
    return ObserveOutcome(run, reports, new_events, new_findings, resolved)


def _unique(events) -> list[Event]:
    seen: dict[str, Event] = {}
    for event in events:
        seen.setdefault(event.dedupe_key, event)
    return list(seen.values())


def _unique_findings(findings) -> list[Finding]:
    seen: dict[str, Finding] = {}
    for finding in findings:
        seen.setdefault(finding.id, finding)
    return list(seen.values())
