"""Criteri di uscita di M1: due esecuzioni e un riavvio non perdono cursori ne' duplicano eventi."""
from datetime import timedelta

import pytest

from factory import NOW, github_fixtures, issue, post, pull, run, sources
from supervisor.collectors.github import GitHubApi, GitHubCollector
from supervisor.collectors.http import FixtureHttp
from supervisor.collectors.promo import PromoCollector, StaticPostReader
from supervisor.core.clock import parse_iso
from supervisor.core.pipeline import LOCK_NAME, LockBusy, observe
from supervisor.state.store import MemoryStore

now = parse_iso(NOW)


def fixtures():
    return github_fixtures(
        runs={"ci.yml": [run(101, "failure"), run(100)], "deploy.yml": [run(200, workflow="deploy.yml")]},
        issues=[issue(1), issue(2, "closed", pr=True, merged=True)], pulls=[pull(3)],
    )


def collectors(fx=None, posts=None):
    srcs = sources()
    http = FixtureHttp(fx or fixtures())
    # Bozza preparata il 26 per oggi (28): vecchia (promo_drafts_stale) ma le bozze di oggi ci sono.
    default = [post("p1", created_for="2026-09-28", scheduled_for="2026-09-28T10:00:00Z"),
               post("p2", "published")]
    promo_reader = StaticPostReader(posts if posts is not None else default)
    return [GitHubCollector(srcs.github[0], GitHubApi(http, "tok")), PromoCollector(srcs.promo, promo_reader)]


def snapshot_state(store):
    return (
        sorted(e.dedupe_key for e in store.events_since("")),
        {k: v["cursor"] for k, v in store.cursors().items()},
        sorted(f.id for f in store.open_findings()),
    )


def test_due_giri_non_duplicano(store):
    first = observe(store, collectors(), sources(), now)
    assert first.run.status == "completed"
    assert first.run.new_events > 0
    assert {f.rule for f in first.new_findings} == {"ci_failed", "pr_without_green_ci", "promo_drafts_stale"}
    state = snapshot_state(store)
    second = observe(store, collectors(), sources(), now + timedelta(hours=1))
    assert second.run.new_events == 0 and second.new_findings == [] and second.resolved == []
    assert snapshot_state(store)[0] == state[0] and snapshot_state(store)[2] == state[2]
    assert store.last_run().id == second.run.id
    assert store.get_lock(LOCK_NAME) is None


class CrashingStore(MemoryStore):
    """Muore al secondo commit: il primo flusso e' confermato, gli altri no."""

    def __init__(self):
        super().__init__()
        self.commits = 0
        self.crash = True

    def commit_stream(self, stream, events, cursor):
        self.commits += 1
        if self.crash and self.commits == 2:
            raise RuntimeError("processo interrotto")
        return super().commit_stream(stream, events, cursor)


def test_riavvio_dopo_un_crash_converge_allo_stato_pulito():
    crashing = CrashingStore()
    with pytest.raises(RuntimeError):
        observe(crashing, collectors(), sources(), now)
    assert crashing.last_run().status == "failed"
    crashing.crash = False
    # Il lock e' stato rilasciato nel finally: il giro successivo parte subito.
    observe(crashing, collectors(), sources(), now)

    clean = MemoryStore()
    observe(clean, collectors(), sources(), now)
    assert snapshot_state(crashing) == snapshot_state(clean)


def test_lock_occupato():
    store = MemoryStore()
    store.acquire_lock(LOCK_NAME, "altro", 900, now)
    with pytest.raises(LockBusy):
        observe(store, collectors(), sources(), now)
    assert store.last_run() is None


def test_lock_scaduto_si_recupera():
    store = MemoryStore()
    store.acquire_lock(LOCK_NAME, "morto", 900, now - timedelta(hours=1))
    assert observe(store, collectors(), sources(), now).run.status == "completed"


def test_dry_run_non_scrive_nulla():
    store = MemoryStore()
    outcome = observe(store, collectors(), sources(), now, dry_run=True)
    assert outcome.run.new_events > 0 and outcome.new_findings
    assert store.events_since("") == [] and store.cursors() == {} and store.last_run() is None
    assert store.latest_snapshot() is None


def test_sorgente_non_raggiungibile_rende_il_run_parziale():
    class Broken:
        def list_posts(self):
            raise PermissionError("403 Missing or insufficient permissions")

    srcs = sources()
    store = MemoryStore()
    cols = [GitHubCollector(srcs.github[0], GitHubApi(FixtureHttp(fixtures()), "tok")),
            PromoCollector(srcs.promo, Broken())]
    outcome = observe(store, cols, srcs, now)
    assert outcome.run.status == "partial"
    assert outcome.run.sources["promo:promo_posts"] == "non disponibile"
    assert "source_unavailable" in {f.rule for f in outcome.new_findings}


def test_ci_tornata_verde_risolve_il_finding():
    store = MemoryStore()
    observe(store, collectors(), sources(), now)
    green = github_fixtures(runs={"ci.yml": [run(102), run(101, "failure")], "deploy.yml": [run(200, workflow="deploy.yml")]},
                            issues=[], pulls=[])
    outcome = observe(store, collectors(green, posts=[]), sources(), now + timedelta(hours=1))
    resolved_rules = {f.rule for f in store.findings_since("") if f.id in outcome.resolved}
    assert resolved_rules == {"ci_failed", "pr_without_green_ci", "promo_drafts_stale"}
    assert store.open_findings() == []
