"""Le forme dei dati del supervisore.

- `Event`: un fatto osservato su una sorgente (una run di CI conclusa, una PR aperta, un post
  Promo che cambia stato). La chiave di deduplicazione non dipende dal momento della raccolta:
  lo stesso fatto raccolto due volte e' lo stesso evento.
- `StreamResult`: quello che un collector ha prodotto per un flusso (repo + parte), con il nuovo
  cursore. Un flusso fallito non avanza il cursore.
- `SourceReport`: lo stato attuale di una sorgente (fatti, non eventi) e la sua completezza.
- `Finding`: un problema riconosciuto dalle regole, con le evidenze.
- `Run`: un'esecuzione di `observe`.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SEVERITIES = ("alta", "media", "bassa")


def stable_hash(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Event:
    source: str
    type: str
    source_id: str
    state: str
    occurred_at: str
    received_at: str
    data: dict = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return stable_hash(self.source, self.type, self.source_id, self.state)

    def to_dict(self) -> dict:
        return {**asdict(self), "dedupe_key": self.dedupe_key}

    @classmethod
    def from_dict(cls, data: dict) -> Event:
        names = ("source", "type", "source_id", "state", "occurred_at", "received_at")
        return cls(**{k: data[k] for k in names}, data=dict(data.get("data") or {}))


@dataclass
class StreamResult:
    stream: str
    events: list[Event] = field(default_factory=list)
    cursor: Optional[dict] = None
    ok: bool = True
    error: str = ""


@dataclass
class SourceReport:
    source: str
    kind: str
    ok: bool
    facts: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    configured: bool = True

    @property
    def completeness(self) -> str:
        if not self.configured:
            return "non configurata"
        if self.ok and not self.errors:
            return "completa"
        return "incompleta" if self.facts else "non disponibile"

    def to_dict(self) -> dict:
        return {**asdict(self), "completeness": self.completeness}

    @classmethod
    def from_dict(cls, data: dict) -> SourceReport:
        return cls(
            source=data["source"], kind=data["kind"], ok=data["ok"], facts=dict(data.get("facts") or {}),
            errors=list(data.get("errors") or []), configured=data.get("configured", True),
        )


@dataclass
class CollectResult:
    streams: list[StreamResult]
    report: SourceReport


@dataclass
class Finding:
    rule: str
    subject: str
    key: str
    statement: str
    severity: str
    evidence: list[str] = field(default_factory=list)
    created_at: str = ""
    run_id: str = ""
    resolved_at: Optional[str] = None
    # Le regole "di stato" si risolvono da sole quando la condizione sparisce.
    stateful: bool = False

    @property
    def id(self) -> str:
        return stable_hash(self.rule, self.subject, self.key)

    def to_dict(self) -> dict:
        return {**asdict(self), "id": self.id}

    @classmethod
    def from_dict(cls, data: dict) -> Finding:
        return cls(**{k: v for k, v in data.items() if k != "id"})


@dataclass
class Run:
    id: str
    started_at: str
    finished_at: Optional[str] = None
    status: str = "running"
    dry_run: bool = False
    new_events: int = 0
    new_findings: list[str] = field(default_factory=list)
    resolved_findings: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Run:
        return cls(**data)
