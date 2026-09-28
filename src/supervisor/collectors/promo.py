"""Collector Promo Studio, in sola lettura sulla coda `promo_posts` del Firestore del gioco.

Il contratto e' quello di `promo_studio/promo/models.py` e `promo/queue.py` (stati draft,
approved, rejected, published, failed; date ISO UTC). Il supervisore non importa codice di
Promo e non scrive mai nella coda: approvare e pubblicare restano azioni di Michele e del
publisher di Promo. Si leggono solo stato, date e id; caption e media non servono.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

from supervisor.core.clock import hours_between, iso, parse_iso
from supervisor.core.config import PromoConfig
from supervisor.core.models import CollectResult, Event, SourceReport, StreamResult
from supervisor.core.scrub import scrub, untrusted

STATUSES = ("draft", "approved", "rejected", "published", "failed")
# Un post approvato con `scheduled_for` passato da piu' di tanto non e' stato pubblicato.
PUBLISH_GRACE = timedelta(hours=2)


class PostReader(Protocol):
    def list_posts(self) -> list[dict]: ...


class FirestorePostReader:
    def __init__(self, project: str, collection: str) -> None:
        self.project = project
        self.collection = collection

    def list_posts(self) -> list[dict]:
        from supervisor.state.firestore import client

        return [s.to_dict() for s in client(self.project).collection(self.collection).stream()]


class StaticPostReader:
    def __init__(self, posts: list[dict]) -> None:
        self.posts = posts

    def list_posts(self) -> list[dict]:
        return [dict(p) for p in self.posts]


class PromoCollector:
    kind = "promo"

    def __init__(self, config: PromoConfig, reader: Optional[PostReader]) -> None:
        self.config = config
        self.reader = reader
        self.source = config.source

    def collect(self, cursors: dict[str, Optional[dict]], now: datetime) -> CollectResult:
        stream = f"{self.source}#posts"
        if self.reader is None:
            report = SourceReport(self.source, self.kind, ok=False, configured=False)
            return CollectResult([], report)
        try:
            posts = self.reader.list_posts()
        except Exception as exc:  # Firestore irraggiungibile o permessi mancanti
            error = scrub(f"{type(exc).__name__}: {exc}")[:300]
            return CollectResult([StreamResult(stream, ok=False, error=error)],
                                 SourceReport(self.source, self.kind, ok=False, errors=[f"posts: {error}"]))
        received = iso(now)
        events = [_post_event(self.source, p, received) for p in posts if p.get("id")]
        facts = _facts(posts, now, self.config.stale_draft_hours)
        return CollectResult([StreamResult(stream, events, {"scanned_at": received, "posts": len(posts)})],
                             SourceReport(self.source, self.kind, ok=True, facts=facts))


def _post_event(source: str, post: dict, received: str) -> Event:
    status = post.get("status", "unknown")
    history = post.get("history") or []
    occurred = (history[-1].get("at") if history else None) or post.get("created_at") or received
    state = f"failed#{post.get('attempts', 0)}" if status == "failed" else status
    return Event(source, "promo_post", str(post["id"]), state, occurred, received, {
        "format": post.get("format"), "language": post.get("language"), "channel": post.get("channel"),
        "scheduled_for": post.get("scheduled_for"), "external_url": post.get("external_url"),
    })


def _facts(posts: list[dict], now: datetime, stale_hours: float) -> dict[str, Any]:
    counts = Counter(p.get("status", "unknown") for p in posts)
    week_ago = iso(now - timedelta(days=7))
    stale, failed, overdue = [], [], []
    for post in posts:
        status = post.get("status")
        if status == "draft" and post.get("created_at"):
            age = hours_between(post["created_at"], now)
            if age >= stale_hours:
                stale.append({"id": post["id"], "age_hours": round(age, 1)})
        elif status == "failed":
            failed.append({"id": post["id"], "attempts": post.get("attempts", 0),
                           "error": untrusted(post.get("error"), 100)})
        elif status == "approved" and post.get("scheduled_for"):
            if parse_iso(post["scheduled_for"]) + PUBLISH_GRACE <= now:
                overdue.append({"id": post["id"], "scheduled_for": post["scheduled_for"]})
    return {
        "total": len(posts),
        "by_status": {s: counts.get(s, 0) for s in STATUSES},
        "stale_drafts": sorted(stale, key=lambda d: d["id"]),
        "stale_draft_hours": stale_hours,
        "failed": sorted(failed, key=lambda d: d["id"]),
        "approved_overdue": sorted(overdue, key=lambda d: d["id"]),
        "published_last_7d": sum(1 for p in posts if p.get("status") == "published"
                                 and (p.get("published_at") or "") >= week_ago),
    }
