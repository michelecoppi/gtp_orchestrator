"""Coda dei task engineering, con lease e un solo task attivo per repository.

Stati (specifica, sez. 7.3): queued, running, awaiting_approval, completed, failed, blocked.
La fase dice dove si trova il lavoro:
- `work`        il worker prepara la patch (stato running);
- `patch_ready` patch pronta e verificata, in attesa dell'executor (running);
- `ci_pending`  draft PR aperta, CI in corso sullo SHA di testa (awaiting_approval);
- `ci_green`    CI verde sullo SHA di testa: tocca a Michele rivedere e fare merge (awaiting_approval);
- `done`        chiuso (completed, failed o blocked).

Il documento `eng_locks/{repo}` indica il task attivo del repository: si prende quando un task viene
reclamato e si libera, nella stessa transazione, quando il task arriva a uno stato finale. Un lease
scaduto rende il lavoro recuperabile, non autorizza a ripetere un effetto esterno: l'executor controlla
sempre se branch o PR esistono gia'.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from supervisor.core.clock import iso, parse_iso
from supervisor.state.store import StateStore

TASKS = "tasks"
ENG_LOCKS = "eng_locks"
STATES = ("queued", "running", "awaiting_approval", "completed", "failed", "blocked")
TERMINAL = ("completed", "failed", "blocked")
ACTIVE = ("running", "awaiting_approval")
MAX_HISTORY = 50


class TaskConflict(RuntimeError):
    """Il task non e' nello stato atteso: qualcun altro l'ha gia' fatto avanzare."""


def task_id_for(repo: str, issue: int, approval_event_id: int) -> str:
    return f"eng-{repo.split('/')[-1]}-{issue}-{approval_event_id}"


def lock_id(repo: str) -> str:
    return repo.replace("/", "__")


def _history(task: dict, at: str, note: str) -> list:
    entry = {"at": at, "state": task["state"], "phase": task.get("phase"), "note": note[:300]}
    return (list(task.get("history") or []) + [entry])[-MAX_HISTORY:]


class TaskQueue:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def get(self, task_id: str) -> Optional[dict]:
        return self.store.get_doc(TASKS, task_id)

    def list(self, state: Optional[str] = None) -> list[dict]:
        docs = self.store.query_docs(TASKS, "kind", "engineering")
        return sorted((d for d in docs if state is None or d["state"] == state), key=lambda d: d["created_at"])

    def create(self, task: dict, now: datetime) -> bool:
        at = iso(now)
        doc = {"kind": "engineering", "state": "queued", "phase": "work", "attempts": 0, "lease_owner": None,
               "lease_until": None, "created_at": at, "updated_at": at, "history": [], **task}
        doc["history"] = _history(doc, at, "creato da approvazione")

        def _create(docs):
            if docs[(TASKS, doc["id"])] is not None:
                return {}, False
            return {(TASKS, doc["id"]): doc}, True

        return self.store.transact([(TASKS, doc["id"])], _create)

    def claim_next(self, owner: str, now: datetime, lease_minutes: int, max_attempts: int) -> Optional[dict]:
        at = iso(now)
        candidates = [t for t in self.list() if t["state"] == "queued"
                      or (t["state"] == "running" and t.get("lease_until") and parse_iso(t["lease_until"]) <= now)]
        for candidate in candidates:
            refs = [(TASKS, candidate["id"]), (ENG_LOCKS, lock_id(candidate["repo"]))]

            def _claim(docs, task_id=candidate["id"]):
                task = docs[(TASKS, task_id)]
                lock = docs[(ENG_LOCKS, lock_id(task["repo"]))]
                expired = task["state"] == "running" and parse_iso(task["lease_until"]) <= now
                if task["state"] != "queued" and not expired:
                    return {}, None
                if lock and lock.get("task_id") not in (None, task_id):
                    return {}, None  # un altro task e' attivo su questo repository
                attempts = task.get("attempts", 0) + 1
                if attempts > max_attempts:
                    task = {**task, "state": "failed", "phase": "done", "updated_at": at, "lease_owner": None,
                            "error": f"superati {max_attempts} tentativi (lease scaduti)"}
                    task["history"] = _history(task, at, task["error"])
                    return {(TASKS, task_id): task, (ENG_LOCKS, lock_id(task["repo"])): {"task_id": None}}, None
                note = "ripreso dopo lease scaduto" if expired else "reclamato"
                task = {**task, "state": "running", "phase": "work", "attempts": attempts, "lease_owner": owner,
                        "lease_until": iso(now + timedelta(minutes=lease_minutes)), "updated_at": at}
                task["history"] = _history(task, at, note)
                return {(TASKS, task_id): task,
                        (ENG_LOCKS, lock_id(task["repo"])): {"task_id": task_id, "since": at}}, task

            claimed = self.store.transact(refs, _claim)
            if claimed:
                return claimed
        return None

    def transition(self, task_id: str, *, expect_states: tuple[str, ...], now: datetime, note: str,
                   state: Optional[str] = None, phase: Optional[str] = None,
                   expect_phase: Optional[str] = None, **fields: Any) -> dict:
        at = iso(now)
        current = self.get(task_id)
        if current is None:
            raise KeyError(task_id)
        refs = [(TASKS, task_id), (ENG_LOCKS, lock_id(current["repo"]))]

        def _move(docs):
            task = docs[(TASKS, task_id)]
            if task["state"] not in expect_states or (expect_phase and task.get("phase") != expect_phase):
                raise TaskConflict(f"{task_id}: stato {task['state']}/{task.get('phase')}, atteso "
                                   f"{'/'.join(expect_states)}{'/' + expect_phase if expect_phase else ''}")
            task = {**task, **fields, "updated_at": at}
            if state:
                task["state"] = state
            if phase:
                task["phase"] = phase
            writes = {(TASKS, task_id): task}
            if task["state"] in TERMINAL:
                task["phase"] = "done"
                task["lease_owner"] = None
                lock = docs[(ENG_LOCKS, lock_id(task["repo"]))]
                if lock and lock.get("task_id") == task_id:
                    writes[(ENG_LOCKS, lock_id(task["repo"]))] = {"task_id": None, "since": at}
            task["history"] = _history(task, at, note)
            return writes, task

        return self.store.transact(refs, _move)
