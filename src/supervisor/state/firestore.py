"""Stato su Firestore, nel progetto GCP dedicato al supervisore (docs/adr/0001-stato-firestore.md).

Separato dal database del gioco e dalle sue credenziali. Gli eventi si creano con `create()`
(fallisce se l'id esiste gia'), il cursore si scrive solo dopo tutti gli eventi del batch: se il
processo muore nel mezzo, il giro successivo rilegge gli stessi fatti e la creazione condizionata
scarta i duplicati. Lock e notifiche passano da transazioni.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from supervisor.core.clock import parse_iso
from supervisor.core.models import Event, Finding, Run
from supervisor.state.store import (
    CURSORS,
    EVENTS,
    FINDINGS,
    LOCKS,
    NOTIFICATIONS,
    RESOLVED_FINDING_DAYS,
    RETENTION_DAYS,
    RUNS,
    SNAPSHOTS,
    lock_doc,
    lock_is_free,
    snapshot_doc,
)


def expire_at(collection: str, reference: Optional[str] = None) -> datetime:
    """Scadenza (Timestamp) per la policy TTL di Firestore, calcolata dalla data del documento."""
    base = parse_iso(reference) if reference else datetime.now(timezone.utc)
    return base + timedelta(days=RETENTION_DAYS[collection])


def client(project: str) -> Any:
    from google.cloud import firestore  # type: ignore[attr-defined]

    return firestore.Client(project=project or None)


def _where(field: str, op: str, value: Any) -> Any:
    from google.cloud.firestore_v1.base_query import FieldFilter

    return FieldFilter(field, op, value)


class FirestoreStore:
    def __init__(self, db: Any, prefix: str = "") -> None:
        self.db = db
        self.prefix = prefix

    def _col(self, name: str) -> Any:
        return self.db.collection(self.prefix + name)

    def _transactional(self, fn):
        from google.cloud import firestore  # type: ignore[attr-defined]

        return firestore.transactional(fn)(self.db.transaction())

    # --- cursori ed eventi -----------------------------------------------------------------
    def get_cursor(self, stream: str) -> Optional[dict]:
        snap = self._col(CURSORS).document(_doc_id(stream)).get()
        return snap.to_dict()["cursor"] if snap.exists else None

    def cursors(self) -> dict[str, dict]:
        return {d["stream"]: d for d in (s.to_dict() for s in self._col(CURSORS).stream())}

    def commit_stream(self, stream, events, cursor):
        from google.api_core.exceptions import AlreadyExists

        inserted = []
        for event in events:
            try:
                self._col(EVENTS).document(event.dedupe_key).create(
                    {**event.to_dict(), "expire_at": expire_at(EVENTS, event.received_at)})
                inserted.append(event)
            except AlreadyExists:
                pass
        if cursor is not None:
            self._col(CURSORS).document(_doc_id(stream)).set({"stream": stream, "cursor": cursor})
        return inserted

    def existing_events(self, keys):
        refs = [self._col(EVENTS).document(k) for k in keys]
        return {s.id for s in self.db.get_all(refs) if s.exists} if refs else set()

    def events_since(self, since):
        query = self._col(EVENTS).where(filter=_where("received_at", ">=", since))
        events = [Event.from_dict(s.to_dict()) for s in query.stream()]
        return sorted(events, key=lambda e: (e.occurred_at, e.dedupe_key))

    # --- finding ---------------------------------------------------------------------------
    def add_findings(self, findings):
        created = []
        for finding in findings:
            ref = self._col(FINDINGS).document(finding.id)

            def _add(transaction, ref=ref, finding=finding):
                snap = ref.get(transaction=transaction)
                if snap.exists and snap.to_dict().get("resolved_at") is None:
                    return False
                transaction.set(ref, finding.to_dict())
                return True

            if self._transactional(_add):
                created.append(finding)
        return created

    def existing_findings(self, ids):
        refs = [self._col(FINDINGS).document(i) for i in ids]
        snaps = self.db.get_all(refs) if refs else []
        return {s.id for s in snaps if s.exists and s.to_dict().get("resolved_at") is None}

    def open_findings(self):
        query = self._col(FINDINGS).where(filter=_where("resolved_at", "==", None))
        return sorted((Finding.from_dict(s.to_dict()) for s in query.stream()), key=lambda f: (f.created_at, f.id))

    def findings_since(self, since):
        query = self._col(FINDINGS).where(filter=_where("created_at", ">=", since))
        return sorted((Finding.from_dict(s.to_dict()) for s in query.stream()), key=lambda f: (f.created_at, f.id))

    def resolve_findings(self, ids, at):
        resolved = []
        for finding_id in ids:
            ref = self._col(FINDINGS).document(finding_id)

            def _resolve(transaction, ref=ref):
                snap = ref.get(transaction=transaction)
                if not snap.exists or snap.to_dict().get("resolved_at") is not None:
                    return False
                transaction.update(ref, {"resolved_at": at,
                                         "expire_at": parse_iso(at) + timedelta(days=RESOLVED_FINDING_DAYS)})
                return True

            if self._transactional(_resolve):
                resolved.append(finding_id)
        return resolved

    # --- run e snapshot --------------------------------------------------------------------
    def save_run(self, run):
        self._col(RUNS).document(run.id).set({**run.to_dict(), "expire_at": expire_at(RUNS, run.started_at)})

    def last_run(self):
        from google.cloud import firestore  # type: ignore[attr-defined]

        docs = list(self._col(RUNS).order_by("started_at", direction=firestore.Query.DESCENDING).limit(1).stream())
        return Run.from_dict(docs[0].to_dict()) if docs else None

    def save_snapshot(self, run_id, at, reports):
        self._col(SNAPSHOTS).document(run_id).set({**snapshot_doc(run_id, at, reports),
                                                   "expire_at": expire_at(SNAPSHOTS, at)})

    def latest_snapshot(self):
        from google.cloud import firestore  # type: ignore[attr-defined]

        query = self._col(SNAPSHOTS).order_by("collected_at", direction=firestore.Query.DESCENDING).limit(1)
        docs = list(query.stream())
        return docs[0].to_dict() if docs else None

    # --- lock --------------------------------------------------------------------------------
    def acquire_lock(self, name, owner, ttl_seconds, now):
        ref = self._col(LOCKS).document(name)

        def _acquire(transaction):
            snap = ref.get(transaction=transaction)
            if not lock_is_free(snap.to_dict() if snap.exists else None, owner, now):
                return False
            transaction.set(ref, {**lock_doc(owner, ttl_seconds, now), "expire_at": expire_at(LOCKS)})
            return True

        return self._transactional(_acquire)

    def release_lock(self, name, owner):
        ref = self._col(LOCKS).document(name)

        def _release(transaction):
            snap = ref.get(transaction=transaction)
            if snap.exists and snap.to_dict().get("owner") == owner:
                transaction.delete(ref)

        self._transactional(_release)

    def get_lock(self, name):
        snap = self._col(LOCKS).document(name).get()
        return snap.to_dict() if snap.exists else None

    # --- notifiche -----------------------------------------------------------------------------
    def claim_notification(self, key, at, summary):
        ref = self._col(NOTIFICATIONS).document(key)

        def _claim(transaction):
            snap = ref.get(transaction=transaction)
            if snap.exists and snap.to_dict().get("status") != "failed":
                return False
            transaction.set(ref, {"key": key, "status": "pending", "claimed_at": at, "summary": summary,
                                  "expire_at": expire_at(NOTIFICATIONS, at)})
            return True

        return self._transactional(_claim)

    def finish_notification(self, key, status, at):
        self._col(NOTIFICATIONS).document(key).set({"status": status, "finished_at": at}, merge=True)

    def pending_notifications(self):
        query = self._col(NOTIFICATIONS).where(filter=_where("status", "==", "pending"))
        return [s.to_dict() for s in query.stream()]

    # --- primitive generiche -------------------------------------------------------------------
    def get_doc(self, collection, doc_id):
        snap = self._col(collection).document(_doc_id(doc_id)).get()
        return snap.to_dict() if snap.exists else None

    def put_doc(self, collection, doc_id, doc, ts=""):
        self._col(collection).document(_doc_id(doc_id)).set(doc)

    def query_docs(self, collection, field, value):
        query = self._col(collection).where(filter=_where(field, "==", value))
        return [s.to_dict() for s in query.stream()]

    def prune_expired(self, now, limit=500):
        """Cancella i documenti con `expire_at` passato, a lotti (senza policy TTL, che richiede la fatturazione)."""
        removed: dict[str, int] = {}
        for collection in (*RETENTION_DAYS, FINDINGS):
            query = self._col(collection).where(filter=_where("expire_at", "<", now)).limit(limit)
            refs = [snap.reference for snap in query.stream()]
            for start in range(0, len(refs), 400):
                batch = self.db.batch()
                for ref in refs[start:start + 400]:
                    batch.delete(ref)
                batch.commit()
            removed[collection] = len(refs)
        return removed

    def transact(self, refs, fn):
        """Letture tutte prima delle scritture, come richiede Firestore; ritentata sui conflitti."""
        doc_refs = {ref: self._col(ref[0]).document(_doc_id(ref[1])) for ref in refs}

        def _run(transaction):
            docs = {}
            for ref, doc_ref in doc_refs.items():
                snap = doc_ref.get(transaction=transaction)
                docs[ref] = snap.to_dict() if snap.exists else None
            writes, result = fn(docs)
            for (collection, doc_id), doc in writes.items():
                target = doc_refs.get((collection, doc_id)) or self._col(collection).document(_doc_id(doc_id))
                transaction.set(target, doc)
            return result

        return self._transactional(_run)


def _doc_id(stream: str) -> str:
    """Gli id Firestore non possono contenere '/': `github:owner/repo#runs` diventa sicuro."""
    return stream.replace("/", "__")
