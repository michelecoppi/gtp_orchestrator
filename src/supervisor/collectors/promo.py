"""Collector Promo Studio, in sola lettura sulla coda `promo_posts` del Firestore del gioco.

Il contratto e' lo schema pubblicato da Promo (`docs/schemas/promo_post.v1.json`), copiato in
`tests/contracts/` con lo SHA del commit di origine (issue #11): stati draft, approved, rejected, published,
failed; date ISO UTC con Z; `created_for` = giorno di Roma delle bozze. I campi letti qui sono in
`POST_FIELDS` e `HISTORY_FIELDS`; `tests/test_promo_contract.py` controlla che siano tutti nello schema e
`python -m supervisor contracts check` dice se lo schema su `main` di Promo e' cambiato.

Da settembre 2026 i lavori di Promo li avvia Cloud Scheduler (via il servizio `promo-approvals`), non piu'
i cron di GitHub. Se si ferma quella catena non fallisce nessuna run da osservare. Per questo il
supervisore controlla anche i dati: le bozze di oggi devono esistere entro `drafts_expected_by`. Il supervisore non importa codice di
Promo e non scrive mai nella coda: approvare e pubblicare restano azioni di Michele e del
publisher di Promo. Si leggono solo stato, date e id; caption e media non servono.

Dall'issue #9 si leggono anche le decisioni sui brief del supervisore (`promo_brief_decisions`, scritte da
Promo quando Michele preme ✅ Usa / ❌ Scarta): servono al brief quotidiano per dire che fine ha fatto ogni
campagna proposta. Se la lettura fallisce, i post restano validi e le decisioni risultano "non disponibili".
Anche questa collezione ha il suo schema di Promo (`promo_brief_decision.v1.json`, issue #14), copiato e
verificato come quello dei post.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol

from supervisor.core.clock import ROME, hours_between, iso, parse_iso, rome_day
from supervisor.core.config import PromoConfig
from supervisor.core.models import CollectResult, Event, SourceReport, StreamResult
from supervisor.core.scrub import scrub, untrusted

STATUSES = ("draft", "approved", "rejected", "published", "failed")
# Campi di `promo_posts` letti dal collector (e di `history[]`): devono esistere nello schema di Promo.
POST_FIELDS = ("id", "status", "created_at", "created_for", "scheduled_for", "published_at", "history",
               "attempts", "error", "format", "language", "channel", "external_url")
HISTORY_FIELDS = ("at",)
# Un post approvato con `scheduled_for` passato da piu' di tanto non e' stato pubblicato.
PUBLISH_GRACE = timedelta(hours=2)
# Collezione di Promo (promo/supervisor_briefs.py) con l'esito dei brief del supervisore. Il contratto e'
# `docs/schemas/promo_brief_decision.v1.json` di Promo, copiato in `tests/contracts/` (issue #14): i campi letti
# qui sono in `DECISION_FIELDS` e devono esistere nello schema (`tests/test_brief_decision_contract.py`).
BRIEF_DECISIONS = "promo_brief_decisions"
DECISION_FIELDS = ("campaign_id", "status", "asked_at", "decided_at", "imported_for")
BRIEF_WINDOW = timedelta(days=30)


class PostReader(Protocol):
    def list_posts(self) -> list[dict]: ...


class FirestorePostReader:
    def __init__(self, project: str, collection: str) -> None:
        self.project = project
        self.collection = collection

    def list_posts(self) -> list[dict]:
        from supervisor.state.firestore import client

        return [s.to_dict() for s in client(self.project).collection(self.collection).stream()]

    def list_brief_decisions(self) -> list[dict]:
        from supervisor.state.firestore import client

        return [s.to_dict() for s in client(self.project).collection(BRIEF_DECISIONS).stream()]


class StaticPostReader:
    def __init__(self, posts: list[dict], decisions: Optional[list[dict]] = None) -> None:
        self.posts = posts
        self.decisions = decisions

    def list_posts(self) -> list[dict]:
        return [dict(p) for p in self.posts]

    def list_brief_decisions(self) -> list[dict]:
        if self.decisions is None:
            raise LookupError("decisioni sui brief non fornite")
        return [dict(d) for d in self.decisions]


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
        facts.update(_daily(posts, now, self.config))
        facts["supervisor_briefs"] = self._briefs(now)
        return CollectResult([StreamResult(stream, events, {"scanned_at": received, "posts": len(posts)})],
                             SourceReport(self.source, self.kind, ok=True, facts=facts))


    def _briefs(self, now: datetime) -> Optional[list[dict]]:
        """Esito dei brief del supervisore negli ultimi 30 giorni; None se non leggibile."""
        reader = getattr(self.reader, "list_brief_decisions", None)
        if reader is None:
            return None
        try:
            docs = reader()
        except Exception:  # collezione illeggibile: non invalida i post
            return None
        since = iso(now - BRIEF_WINDOW)
        recent = [d for d in docs if d.get("campaign_id")
                  and max(d.get("decided_at") or "", d.get("asked_at") or "") >= since]
        return [{"campaign_id": d["campaign_id"], "status": d.get("status", "unknown"),
                 "decided_at": d.get("decided_at"), "imported_for": d.get("imported_for")}
                for d in sorted(recent, key=lambda d: d["campaign_id"])]


def _post_event(source: str, post: dict, received: str) -> Event:
    status = post.get("status", "unknown")
    history = post.get("history") or []
    occurred = (history[-1].get("at") if history else None) or post.get("created_at") or received
    state = f"failed#{post.get('attempts', 0)}" if status == "failed" else status
    return Event(source, "promo_post", str(post["id"]), state, occurred, received, {
        "format": post.get("format"), "language": post.get("language"), "channel": post.get("channel"),
        "scheduled_for": post.get("scheduled_for"), "external_url": post.get("external_url"),
    })


def _daily(posts: list[dict], now: datetime, config: PromoConfig) -> dict[str, Any]:
    """Bozze e pubblicazioni di oggi (giorno di Roma) e se mancano le bozze attese."""
    today = rome_day(now)
    earliest = rome_day(now - timedelta(days=config.active_days))
    days = [p.get("created_for") or "" for p in posts]
    drafts_today = sum(1 for d in days if d == today)
    active = any(earliest <= d < today for d in days)
    hour, minute = (int(x) for x in config.drafts_expected_by.split(":"))
    local = now.astimezone(ROME)
    late = (local.hour, local.minute) >= (hour, minute)
    published_today = sum(1 for p in posts if p.get("status") == "published" and p.get("published_at")
                          and rome_day(parse_iso(p["published_at"])) == today)
    return {
        "today": today,
        "drafts_today": drafts_today,
        "published_today": published_today,
        "drafts_expected_by": config.drafts_expected_by,
        # Solo se Promo era attivo nei giorni scorsi: con PROMO_ENABLED=false non ci sono bozze da attendere.
        "drafts_missing": late and active and drafts_today == 0,
    }


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
