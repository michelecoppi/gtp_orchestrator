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


def test_esito_dei_brief_del_supervisore():
    from supervisor.core.models import SourceReport
    from supervisor.reporting.brief import _promo_section

    decisions = [
        {"campaign_id": "2026w40-whois", "status": "used", "asked_at": "2026-09-28T06:40:00Z",
         "decided_at": "2026-09-28T07:00:00Z", "imported_for": "2026-09-29"},
        {"campaign_id": "2026w41-ladder", "status": "asked", "asked_at": "2026-10-05T06:40:00Z"},
        {"campaign_id": "2026w30-whois", "status": "discarded", "decided_at": "2026-07-20T07:00:00Z"},  # vecchio
    ]
    now = parse_iso("2026-10-05T08:00:00Z")
    result = PromoCollector(PromoConfig(), StaticPostReader(YESTERDAY, decisions)).collect({}, now)
    briefs = result.report.facts["supervisor_briefs"]
    assert [b["campaign_id"] for b in briefs] == ["2026w40-whois", "2026w41-ladder"]
    lines = _promo_section(result.report).lines
    assert lines[-1] == ("Brief del supervisore in Promo (30 giorni): 2026w40-whois usato, bozze del 2026-09-29; "
                         "2026w41-ladder proposto, in attesa di Michele")
    # Senza decisioni leggibili i post restano validi e i brief risultano non disponibili.
    missing = PromoCollector(PromoConfig(), StaticPostReader(YESTERDAY)).collect({}, now).report
    assert missing.ok and missing.facts["supervisor_briefs"] is None
    assert _promo_section(SourceReport(missing.source, "promo", True, missing.facts)).lines[-1].endswith(
        "non disponibile")
