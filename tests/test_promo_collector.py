from supervisor.collectors.promo import PromoCollector, StaticPostReader
from supervisor.core.clock import parse_iso
from supervisor.core.config import PromoConfig


def facts(posts, now):
    result = PromoCollector(PromoConfig(), StaticPostReader(posts)).collect({}, parse_iso(now))
    return result.report.facts


def post(pid, day, status="draft", published_at=None):
    return {"id": pid, "status": status, "created_for": day, "created_at": f"{day}T06:37:00Z",
            "scheduled_for": f"{day}T10:00:00Z", "published_at": published_at}


YESTERDAY = [post("y1", "2026-09-28", "published", "2026-09-28T10:23:00Z")]


def test_bozze_mancanti_solo_dopo_l_orario_e_se_promo_era_attivo():
    # 09:30 di Roma (07:30 UTC, ora legale): ancora in tempo.
    assert facts(YESTERDAY, "2026-09-29T07:30:00Z")["drafts_missing"] is False
    # 10:15 di Roma senza bozze di oggi: mancano.
    late = facts(YESTERDAY, "2026-09-29T08:15:00Z")
    assert late["drafts_missing"] is True and late["drafts_today"] == 0 and late["today"] == "2026-09-29"
    # Con le bozze di oggi, nessun problema.
    ok = facts(YESTERDAY + [post("t1", "2026-09-29")], "2026-09-29T08:15:00Z")
    assert ok["drafts_missing"] is False and ok["drafts_today"] == 1
    # Promo fermo da giorni (o PROMO_ENABLED=false): non ci sono bozze da attendere.
    assert facts([post("o1", "2026-09-20")], "2026-09-29T08:15:00Z")["drafts_missing"] is False


def test_pubblicati_oggi_nel_giorno_di_roma():
    posts = YESTERDAY + [post("t1", "2026-09-29", "published", "2026-09-29T10:23:00Z"),
                         post("t2", "2026-09-29", "published", "2026-09-28T22:30:00Z")]  # 00:30 di Roma
    assert facts(posts, "2026-09-29T12:00:00Z")["published_today"] == 2
