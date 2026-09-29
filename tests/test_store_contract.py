"""Lo stesso contratto per memoria, SQLite e Firestore (emulatore)."""
import threading
from datetime import timedelta

from supervisor.core.clock import parse_iso
from supervisor.core.models import Event, Finding, Run, SourceReport
from supervisor.state.sqlite import SQLiteStore

NOW = parse_iso("2026-09-28T08:00:00Z")


def event(source_id: str, state: str = "open", received: str = "2026-09-28T08:00:00Z") -> Event:
    return Event("github:o/r", "issue", source_id, state, "2026-09-28T07:00:00Z", received, {"title": "x"})


def finding(key: str, created: str = "2026-09-28T08:00:00Z") -> Finding:
    return Finding("ci_failed", "o/r", key, "CI rotta", "alta", ["https://x"], created_at=created, run_id="r1")


def test_eventi_scritti_una_volta_sola_e_cursore_salvato(store):
    first = store.commit_stream("github:o/r#issues", [event("1"), event("2"), event("1")], {"since": "A"})
    assert [e.source_id for e in first] == ["1", "2"]
    again = store.commit_stream("github:o/r#issues", [event("1"), event("2")], {"since": "B"})
    assert again == []
    assert store.get_cursor("github:o/r#issues") == {"since": "B"}
    assert store.existing_events([event("1").dedupe_key, event("9").dedupe_key]) == {event("1").dedupe_key}


def test_cambio_di_stato_e_un_evento_nuovo(store):
    store.commit_stream("s#x", [event("1", "open")], None)
    assert len(store.commit_stream("s#x", [event("1", "closed")], None)) == 1
    assert store.get_cursor("s#x") is None


def test_events_since_filtra_per_ricezione(store):
    store.commit_stream("s#x", [event("1", received="2026-09-27T08:00:00Z"), event("2")], None)
    assert [e.source_id for e in store.events_since("2026-09-28T00:00:00Z")] == ["2"]


def test_finding_creato_una_volta_risolto_e_riaperto(store):
    assert len(store.add_findings([finding("a")])) == 1
    assert store.add_findings([finding("a")]) == []
    assert [f.key for f in store.open_findings()] == ["a"]
    assert store.resolve_findings([finding("a").id], "2026-09-28T09:00:00Z") == [finding("a").id]
    assert store.resolve_findings([finding("a").id], "2026-09-28T09:00:00Z") == []
    assert store.open_findings() == []
    assert store.existing_findings([finding("a").id]) == set()
    reopened = store.add_findings([finding("a", created="2026-09-28T10:00:00Z")])
    assert len(reopened) == 1 and store.open_findings()[0].resolved_at is None
    assert [f.key for f in store.findings_since("2026-09-28T09:30:00Z")] == ["a"]


def test_ultimo_run_e_ultimo_snapshot(store):
    store.save_run(Run(id="r1", started_at="2026-09-28T07:00:00Z", status="completed"))
    store.save_run(Run(id="r2", started_at="2026-09-28T08:00:00Z", status="partial"))
    assert store.last_run().id == "r2"
    report = SourceReport("github:o/r", "github", True, {"repo": "o/r"})
    store.save_snapshot("r1", "2026-09-28T07:00:00Z", [report])
    store.save_snapshot("r2", "2026-09-28T08:00:00Z", [report])
    snapshot = store.latest_snapshot()
    assert snapshot["run_id"] == "r2" and snapshot["sources"][0]["completeness"] == "completa"


def test_lock_con_lease(store):
    assert store.acquire_lock("observe", "a", 60, NOW)
    assert not store.acquire_lock("observe", "b", 60, NOW)
    assert store.acquire_lock("observe", "a", 60, NOW)  # rientrante per lo stesso owner
    store.release_lock("observe", "b")  # chi non lo tiene non lo libera
    assert store.get_lock("observe")["owner"] == "a"
    assert store.acquire_lock("observe", "b", 60, NOW + timedelta(seconds=61))  # lease scaduto
    store.release_lock("observe", "b")
    assert store.get_lock("observe") is None


def test_notifica_prenotata_una_volta(store):
    assert store.claim_notification("k", "2026-09-28T08:00:00Z", "testo")
    assert not store.claim_notification("k", "2026-09-28T08:01:00Z", "testo")
    assert [d["key"] for d in store.pending_notifications()] == ["k"]
    store.finish_notification("k", "failed", "2026-09-28T08:02:00Z")
    assert store.claim_notification("k", "2026-09-28T08:03:00Z", "testo")  # un invio fallito si ritenta
    store.finish_notification("k", "sent", "2026-09-28T08:04:00Z")
    assert not store.claim_notification("k", "2026-09-28T08:05:00Z", "testo")
    assert store.pending_notifications() == []


def test_sqlite_lock_esclusivo_fra_connessioni(tmp_path):
    path = str(tmp_path / "shared.sqlite3")
    stores = [SQLiteStore(path) for _ in range(8)]
    results: list[bool] = []
    barrier = threading.Barrier(len(stores))

    def contend(i: int) -> None:
        barrier.wait()
        results.append(stores[i].acquire_lock("observe", f"owner-{i}", 60, NOW))

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(len(stores))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 7 + [True]


def test_documento_creato_solo_se_assente(store):
    """La primitiva usata per i brief di Promo (`growth.publish_brief`): crea, non sovrascrive."""
    ref = ("promo_briefs", "2026w40-whois")

    def create(doc):
        def _fn(docs):
            return ({}, False) if docs[ref] is not None else ({ref: doc}, True)
        return _fn

    first = {"campaign_id": "2026w40-whois", "status": "proposed", "created_at": "2026-09-28T08:00:00Z",
             "brief": {"campaign_id": "2026w40-whois", "facts": ["a"]}}
    assert store.transact([ref], create(first)) is True
    assert store.transact([ref], create({**first, "status": "altro"})) is False
    saved = store.get_doc(*ref)
    assert saved["status"] == "proposed" and saved["brief"] == first["brief"]
    assert [d["campaign_id"] for d in store.query_docs("promo_briefs", "status", "proposed")] == ["2026w40-whois"]


def test_brief_per_promo_conservati_90_giorni(store):
    old = {"campaign_id": "vecchio", "status": "proposed", "created_at": "2026-06-01T08:00:00Z"}
    new = {"campaign_id": "nuovo", "status": "proposed", "created_at": "2026-09-20T08:00:00Z"}
    for doc in (old, new):
        store.put_doc("promo_briefs", doc["campaign_id"], doc, doc["created_at"])
    assert store.prune_expired(NOW)["promo_briefs"] == 1
    assert [d["campaign_id"] for d in store.query_docs("promo_briefs", "status", "proposed")] == ["nuovo"]
