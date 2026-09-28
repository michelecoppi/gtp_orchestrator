"""Stato persistente del supervisore: eventi, cursori, finding, run, snapshot, lock, notifiche.

Tre implementazioni con lo stesso contratto (tests/test_store_contract.py):
- `MemoryStore` per i test;
- `SQLiteStore` per sviluppo ed evaluation locale;
- `FirestoreStore` in produzione, nel progetto GCP dedicato al supervisore.

Le garanzie che contano:
- un evento si scrive una sola volta (id del documento = `dedupe_key`, creazione condizionata);
- il cursore di un flusso avanza solo insieme agli eventi del suo batch; se il processo muore
  prima, al giro successivo si rileggono gli stessi fatti e la deduplicazione li scarta;
- il lock ha un lease: se chi lo tiene muore, scade e il lavoro torna recuperabile;
- una notifica si "prenota" prima dell'invio; una prenotazione rimasta `pending` (crash durante
  l'invio) non viene ripetuta alla cieca: la si vede in `status`.

Il runner di GitHub Actions ha un disco effimero: SQLite li' non e' una fonte autorevole.
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator, Optional, Protocol

from supervisor.core.clock import iso, parse_iso
from supervisor.core.models import Event, Finding, Run, SourceReport

EVENTS = "events"
CURSORS = "cursors"
FINDINGS = "findings"
RUNS = "runs"
SNAPSHOTS = "snapshots"
LOCKS = "locks"
NOTIFICATIONS = "notifications"


class StateStore(Protocol):
    def get_cursor(self, stream: str) -> Optional[dict]: ...

    def cursors(self) -> dict[str, dict]: ...

    def commit_stream(self, stream: str, events: list[Event], cursor: Optional[dict]) -> list[Event]: ...

    def existing_events(self, keys: list[str]) -> set[str]: ...

    def events_since(self, since: str) -> list[Event]: ...

    def add_findings(self, findings: list[Finding]) -> list[Finding]: ...

    def existing_findings(self, ids: list[str]) -> set[str]: ...

    def open_findings(self) -> list[Finding]: ...

    def findings_since(self, since: str) -> list[Finding]: ...

    def resolve_findings(self, ids: list[str], at: str) -> list[str]: ...

    def save_run(self, run: Run) -> None: ...

    def last_run(self) -> Optional[Run]: ...

    def save_snapshot(self, run_id: str, at: str, reports: list[SourceReport]) -> None: ...

    def latest_snapshot(self) -> Optional[dict]: ...

    def acquire_lock(self, name: str, owner: str, ttl_seconds: int, now: datetime) -> bool: ...

    def release_lock(self, name: str, owner: str) -> None: ...

    def get_lock(self, name: str) -> Optional[dict]: ...

    def claim_notification(self, key: str, at: str, summary: str) -> bool: ...

    def finish_notification(self, key: str, status: str, at: str) -> None: ...

    def pending_notifications(self) -> list[dict]: ...


def snapshot_doc(run_id: str, at: str, reports: list[SourceReport]) -> dict:
    return {"run_id": run_id, "collected_at": at, "sources": [r.to_dict() for r in reports]}


def lock_is_free(current: Optional[dict], owner: str, now: datetime) -> bool:
    if not current:
        return True
    return current.get("owner") == owner or parse_iso(current["lease_until"]) <= now


def lock_doc(owner: str, ttl_seconds: int, now: datetime) -> dict:
    return {"owner": owner, "acquired_at": iso(now), "lease_until": iso(now + timedelta(seconds=ttl_seconds))}


class DocStore:
    """Logica comune a memoria e SQLite, costruita su poche primitive documentali.

    Ogni metodo pubblico che legge e poi scrive lo fa dentro `_atomic()`: un lock di thread in
    memoria, una transazione `BEGIN IMMEDIATE` in SQLite (esclusiva anche fra processi)."""

    # --- primitive -------------------------------------------------------------------------
    @contextmanager
    def _atomic(self) -> Iterator[None]:
        raise NotImplementedError
        yield

    def _get(self, collection: str, doc_id: str) -> Optional[dict]:
        raise NotImplementedError

    def _put(self, collection: str, doc_id: str, doc: dict, ts: str = "") -> None:
        raise NotImplementedError

    def _delete(self, collection: str, doc_id: str) -> None:
        raise NotImplementedError

    def _scan(self, collection: str, ts_from: Optional[str] = None) -> list[dict]:
        raise NotImplementedError

    # --- cursori ed eventi -----------------------------------------------------------------
    def get_cursor(self, stream: str) -> Optional[dict]:
        doc = self._get(CURSORS, stream)
        return doc["cursor"] if doc else None

    def cursors(self) -> dict[str, dict]:
        return {d["stream"]: d for d in self._scan(CURSORS)}

    def commit_stream(self, stream, events, cursor):
        inserted = []
        with self._atomic():
            for event in events:
                if self._get(EVENTS, event.dedupe_key) is None:
                    self._put(EVENTS, event.dedupe_key, event.to_dict(), event.received_at)
                    inserted.append(event)
            if cursor is not None:
                self._put(CURSORS, stream, {"stream": stream, "cursor": cursor})
        return inserted

    def existing_events(self, keys):
        return {k for k in keys if self._get(EVENTS, k) is not None}

    def events_since(self, since):
        docs = self._scan(EVENTS, ts_from=since)
        return sorted((Event.from_dict(d) for d in docs), key=lambda e: (e.occurred_at, e.dedupe_key))

    # --- finding ---------------------------------------------------------------------------
    def add_findings(self, findings):
        created = []
        with self._atomic():
            for finding in findings:
                current = self._get(FINDINGS, finding.id)
                if current is not None and current.get("resolved_at") is None:
                    continue
                # Un finding risolto che si ripresenta e' di nuovo aperto (e di nuovo "nuovo").
                self._put(FINDINGS, finding.id, finding.to_dict(), finding.created_at)
                created.append(finding)
        return created

    def existing_findings(self, ids):
        return {i for i in ids if (doc := self._get(FINDINGS, i)) and doc.get("resolved_at") is None}

    def open_findings(self):
        docs = [d for d in self._scan(FINDINGS) if d.get("resolved_at") is None]
        return sorted((Finding.from_dict(d) for d in docs), key=lambda f: (f.created_at, f.id))

    def findings_since(self, since):
        docs = self._scan(FINDINGS, ts_from=since)
        return sorted((Finding.from_dict(d) for d in docs), key=lambda f: (f.created_at, f.id))

    def resolve_findings(self, ids, at):
        resolved = []
        with self._atomic():
            for finding_id in ids:
                doc = self._get(FINDINGS, finding_id)
                if doc and doc.get("resolved_at") is None:
                    doc["resolved_at"] = at
                    self._put(FINDINGS, finding_id, doc, doc.get("created_at", ""))
                    resolved.append(finding_id)
        return resolved

    # --- run e snapshot --------------------------------------------------------------------
    def save_run(self, run):
        self._put(RUNS, run.id, run.to_dict(), run.started_at)

    def last_run(self):
        docs = self._scan(RUNS)
        return Run.from_dict(max(docs, key=lambda d: (d["started_at"], d["id"]))) if docs else None

    def save_snapshot(self, run_id, at, reports):
        self._put(SNAPSHOTS, run_id, snapshot_doc(run_id, at, reports), at)

    def latest_snapshot(self):
        docs = self._scan(SNAPSHOTS)
        return max(docs, key=lambda d: (d["collected_at"], d["run_id"])) if docs else None

    # --- lock --------------------------------------------------------------------------------
    def acquire_lock(self, name, owner, ttl_seconds, now):
        with self._atomic():
            if not lock_is_free(self._get(LOCKS, name), owner, now):
                return False
            self._put(LOCKS, name, lock_doc(owner, ttl_seconds, now))
            return True

    def release_lock(self, name, owner):
        with self._atomic():
            current = self._get(LOCKS, name)
            if current and current.get("owner") == owner:
                self._delete(LOCKS, name)

    def get_lock(self, name):
        return self._get(LOCKS, name)

    # --- notifiche -----------------------------------------------------------------------------
    def claim_notification(self, key, at, summary):
        with self._atomic():
            current = self._get(NOTIFICATIONS, key)
            if current is not None and current.get("status") != "failed":
                return False
            self._put(NOTIFICATIONS, key, {"key": key, "status": "pending", "claimed_at": at,
                                           "summary": summary}, at)
            return True

    def finish_notification(self, key, status, at):
        with self._atomic():
            doc = self._get(NOTIFICATIONS, key) or {"key": key, "claimed_at": at, "summary": ""}
            doc.update(status=status, finished_at=at)
            self._put(NOTIFICATIONS, key, doc, doc["claimed_at"])

    def pending_notifications(self):
        return [d for d in self._scan(NOTIFICATIONS) if d.get("status") == "pending"]


class MemoryStore(DocStore):
    def __init__(self) -> None:
        self._docs: dict[str, dict[str, tuple[str, dict]]] = {}
        self._lock = threading.RLock()

    @contextmanager
    def _atomic(self):
        with self._lock:
            yield

    def _get(self, collection, doc_id):
        item = self._docs.get(collection, {}).get(doc_id)
        return copy.deepcopy(item[1]) if item else None

    def _put(self, collection, doc_id, doc, ts=""):
        with self._lock:
            self._docs.setdefault(collection, {})[doc_id] = (ts, copy.deepcopy(doc))

    def _delete(self, collection, doc_id):
        with self._lock:
            self._docs.get(collection, {}).pop(doc_id, None)

    def _scan(self, collection, ts_from=None):
        items = list(self._docs.get(collection, {}).values())
        return [copy.deepcopy(doc) for ts, doc in items if ts_from is None or ts >= ts_from]
