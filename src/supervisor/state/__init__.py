"""Apertura dello stato in base a `SUP_STORE`."""
from __future__ import annotations

from supervisor.core.config import ConfigError, Settings
from supervisor.state.store import MemoryStore, StateStore


def open_store(settings: Settings) -> StateStore:
    if settings.store == "memory":
        return MemoryStore()
    if settings.store == "sqlite":
        from supervisor.state.sqlite import SQLiteStore

        return SQLiteStore(settings.sqlite_path)
    if not settings.firestore_project:
        raise ConfigError("SUP_STORE=firestore richiede SUP_FIRESTORE_PROJECT")
    from supervisor.state.firestore import FirestoreStore, client

    return FirestoreStore(client(settings.firestore_project))
