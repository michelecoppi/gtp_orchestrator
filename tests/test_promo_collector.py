import json
from pathlib import Path

from factory import post as factory_post
from supervisor.collectors.promo import PromoCollector, StaticPostReader
from supervisor.core.clock import parse_iso
from supervisor.core.config import PromoConfig


def facts(posts, now):
    result = PromoCollector(PromoConfig(), StaticPostReader(posts)).collect({}, parse_iso(now))
    return result.report.facts


def post(pid, day, status="draft", published_at=None):
    """Conforme a tests/contracts/promo_post.v1.json (vedi test_promo_contract.py)."""
    doc = factory_post(pid, status, f"{day}T06:37:00Z")
    if published_at:
        doc["published_at"] = published_at
    return doc


YESTERDAY = [post("y1", "2026-09-28", "published", "2026-09-28T10:23:00Z")]
# Decisioni di Promo sui brief del supervisore, conformi a tests/contracts/promo_brief_decision.v1.json
# (vedi test_brief_decision_contract.py).
BRIEF_DECISIONS = json.loads((Path(__file__).parent / "fixtures" / "promo_brief_decisions.json")
                             .read_text(encoding="utf-8"))


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

    # Usata, in attesa e una scartata a luglio, fuori dalla finestra di 30 giorni.
    now = parse_iso("2026-10-05T08:00:00Z")
    result = PromoCollector(PromoConfig(), StaticPostReader(YESTERDAY, BRIEF_DECISIONS)).collect({}, now)
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
