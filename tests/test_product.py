import json
from pathlib import Path

import pytest

from ai_factory import gateway, write_ai_config
from supervisor.collectors.http import HttpResponse
from supervisor.collectors.posthog import PostHogCollector
from supervisor.core.clock import parse_iso
from supervisor.core.config import PromoConfig, Sources
from supervisor.core.models import SourceReport
from supervisor.llm.client import FakeLLM
from supervisor.product.growth import (
    PROMO_BRIEFS,
    PROPOSALS,
    baseline_and_volume,
    campaign_code,
    load_promo_facts,
    parse_proposal,
    product_lines,
    review_markdown,
    unverified_numbers,
    weekly_review,
)
from supervisor.product.metrics import HogQL, ProductConfig, collect_product, load_product, rate
from supervisor.product.sample_size import feasibility, per_arm
from supervisor.reporting.brief import build_brief, render_markdown
from supervisor.rules.engine import evaluate
from supervisor.state.store import MemoryStore, snapshot_doc

NOW = parse_iso("2026-09-28T08:00:00Z")
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ProductConfig(host="https://eu.i.posthog.com", project_id="275711")


class FakePostHog:
    """Risponde alle query HogQL riconoscendole da un frammento del testo."""

    def __init__(self, answers: dict[str, list], fail: tuple[str, ...] = ()):
        self.answers = answers
        self.fail = fail
        self.queries: list[str] = []

    def request(self, method, url, params=None, headers=None, json_body=None):
        query = json_body["query"]["query"]
        self.queries.append(query)
        for marker in self.fail:
            if marker in query:
                return HttpResponse(500, {"detail": "boom"})
        for marker, rows in self.answers.items():
            if marker in query:
                return HttpResponse(200, {"results": rows})
        return HttpResponse(200, {"results": []})


BASELINE = {  # la situazione reale del 27/09: niente bot_started, pochi utenti Daily
    "GROUP BY event": [["daily_guess_submitted", 16, 5], ["daily_completed", 9, 5], ["hint_used", 1, 1]],
    "uniqIf(tuple": [[11, 9, 1, 1.44]],
    "toStartOfWeek(f.first_day": [["2026-09-21", 3, 0, 0], ["2026-09-28", 2, 0, 0]],
    "referral_attached = true) AS attached": [[0, 0]],
}


def test_stati_della_lettura():
    assert rate(3, 0, 30)["status"] == "non_disponibile" and rate(3, 0, 30)["value"] is None
    assert rate(9, 11, 30)["status"] == "sotto_soglia" and rate(9, 11, 30)["value"] == pytest.approx(9 / 11)
    assert rate(40, 100, 30)["status"] == "ok"


def test_raccolta_sulla_baseline_reale():
    http = FakePostHog(BASELINE)
    facts = collect_product(HogQL(http, CONFIG, "phx_key"), CONFIG)
    assert facts.errors == []
    assert facts.completion["completion"]["status"] == "sotto_soglia"
    assert facts.completion["completion"]["numerator"] == 9 and facts.completion["attempts_per_completion"] == 1.44
    assert facts.activation_by_channel == [] and facts.north_star == []
    assert all(c["status"] == "immatura" for c in facts.return_cohorts)
    assert facts.referral["status"] == "non_disponibile"
    assert any("bot_started assente" in note for note in facts.data_quality)
    assert all("properties.environment = 'production'" in q for q in http.queries)
    assert any("properties.is_new_user, timestamp) AS new" in q and "s.new = 'true'" in q for q in http.queries)


def test_errori_isolati_e_chiave_mancante():
    facts = collect_product(HogQL(FakePostHog(BASELINE, fail=("uniqIf(tuple",)), CONFIG, "k"), CONFIG)
    assert facts.errors == ["completamento: PostHog HTTP 500 boom"] and facts.volumes
    facts = collect_product(HogQL(FakePostHog(BASELINE), CONFIG, ""), CONFIG)
    assert len(facts.errors) == 6 and "non configurata" in facts.errors[0]


def test_collector_con_cache_e_non_configurato():
    http = FakePostHog(BASELINE)
    collector = PostHogCollector(CONFIG, HogQL(http, CONFIG, "k"))
    first = collector.collect({}, NOW)
    assert first.report.ok and first.streams[0].cursor["facts"]["completion"]
    calls = len(http.queries)
    cursors = {first.streams[0].stream: first.streams[0].cursor}
    again = collector.collect(cursors, parse_iso("2026-09-28T20:00:00Z"))
    assert again.report.facts["cached"] and len(http.queries) == calls  # dentro le 20 ore: nessuna query
    collector.collect(cursors, parse_iso("2026-09-29T08:00:00Z"))
    assert len(http.queries) > calls
    assert PostHogCollector(CONFIG, None).collect({}, NOW).report.completeness == "non configurata"
    failing = PostHogCollector(CONFIG, HogQL(FakePostHog(BASELINE, fail=("GROUP BY event",)), CONFIG, "k"))
    result = failing.collect({}, NOW)
    assert not result.report.ok and result.streams[0].cursor is None


def test_regola_e_brief_prodotto():
    facts = collect_product(HogQL(FakePostHog(BASELINE), CONFIG, "k"), CONFIG).__dict__
    report = SourceReport("posthog:275711", "posthog", True, facts)
    out = evaluate([], [report], [], Sources(github=(), promo=PromoConfig()), NOW, "r1")
    assert [f.rule for f in out.findings] == ["analytics_data_quality"]
    text = render_markdown(build_brief(snapshot_doc("r1", "2026-09-28T08:00:00Z", [report]), [], [], NOW))
    assert "## Prodotto — PostHog (completa)" in text and "QUALITA' DATI: bot_started assente" in text
    assert "completamento 82% (9/11) (sotto soglia: solo descrittivo)" in text
    assert "North Star: non disponibile" in text
    empty = SourceReport("posthog:275711", "posthog", False, configured=False)
    text = render_markdown(build_brief(snapshot_doc("r1", "2026-09-28T08:00:00Z", [empty]), [], [], NOW))
    assert "Metriche non configurate" in text


def test_numerosita_e_fattibilita():
    assert per_arm(0.10, 0.12) == 3841
    small = feasibility(0.3, 0.2, 40, max_weeks=8)
    assert not small.feasible and small.weeks == 49 and "qualitativo" in small.reason
    assert feasibility(0.3, 0.2, 2000).feasible
    assert not feasibility(None, 0.2, 2000).feasible and not feasibility(1.0, 0.2, 2000).feasible


GOOD = {
    "observation": "Mancano gli eventi bot_started mentre arrivano 16 tentativi Daily.",
    "data_coverage": "Quattro giorni di dati, 11 utenti-giorno: sotto soglia.",
    "hypothesis": "La consegna dell'evento /start e' rotta.", "intervention": "Verificare la consegna di bot_started.",
    "role": "promo", "primary_metric": "daily_completion", "relative_lift": 0.2, "guardrail": "Errori del bot.",
    "duration_weeks": 2, "stop_rule": "Stop se gli errori crescono.", "inconclusive_plan": "Raccogliere feedback.",
    "qualitative_alternative": "Chiedere a 5 giocatori dove si fermano; il 73% abbandona.",
    "promo": {"audience": "Appassionati di calcio", "language": "it", "format": "who_is", "channel": "tiktok",
              "cta": "Gioca la sfida di oggi", "angle": "Riconosci il campione dal percorso"},
}


def test_proposta_validata_e_numeri_non_verificati():
    promo = load_promo_facts(ROOT / "config")
    parsed = parse_proposal("Ecco:\n" + json.dumps(GOOD), promo)
    assert parsed["role"] == "promo" and parsed["promo"]["channel"] == "tiktok"
    bad = parse_proposal(json.dumps({**GOOD, "promo": {**GOOD["promo"], "channel": "sito-a-caso", "language": "de"}}),
                         promo)
    assert bad["promo"]["channel"] == "telegram_channel" and bad["promo"]["language"] == "it"
    facts = collect_product(HogQL(FakePostHog(BASELINE), CONFIG, "k"), CONFIG).__dict__
    flagged = unverified_numbers([GOOD["observation"], GOOD["qualitative_alternative"]], facts)
    assert flagged == ["73%"]  # 16 e 11 sono nei dati, il 73% no
    with pytest.raises(ValueError):
        parse_proposal(json.dumps({**GOOD, "primary_metric": "ricavi"}), promo)


def test_review_settimanale_completa(tmp_path):
    store = MemoryStore()
    facts = collect_product(HogQL(FakePostHog(BASELINE), CONFIG, "k"), CONFIG).__dict__
    llm = FakeLLM({"growth_weekly": json.dumps(GOOD)}, 800, 400)
    gw = gateway(store, write_ai_config(tmp_path), llm)
    promo = load_promo_facts(ROOT / "config")
    result = weekly_review(store, gw, facts, CONFIG, promo, "test-model", 2500, NOW)
    assert result.status == "proposed"
    proposal = store.get_doc(PROPOSALS, "2026-W40")
    assert proposal["kind"] == "qualitativa"  # 11 utenti-giorno sotto soglia: nessun esperimento misurabile
    assert "non disponibile o sotto soglia" in proposal["feasibility"]["reason"]
    brief = store.get_doc(PROMO_BRIEFS, "2026w40-whois")
    assert brief["state"] == "draft" and brief["proposed_start_param"] == "src_tiktok-2026w40-whois"
    assert brief["facts"][0].startswith("Ogni giorno una nuova sfida") and brief["warnings"] == []
    text = review_markdown(result)
    assert "## Brief per Promo Studio (bozza)" in text and "numeri non presenti nei dati: 73%" in text
    # Idempotente: stessa settimana, nessuna nuova chiamata.
    assert weekly_review(store, gw, facts, CONFIG, promo, "test-model", 2500, NOW).status == "exists"
    assert len(llm.requests) == 1


def test_review_dry_run_e_senza_modello(tmp_path):
    store = MemoryStore()
    facts = collect_product(HogQL(FakePostHog(BASELINE), CONFIG, "k"), CONFIG).__dict__
    llm = FakeLLM({"growth_weekly": json.dumps(GOOD)})
    promo = load_promo_facts(ROOT / "config")
    dry = weekly_review(store, gateway(store, write_ai_config(tmp_path), llm), facts, CONFIG, promo, "test-model",
                        2500, NOW, dry_run=True)
    assert dry.estimate_usd > 0 and llm.requests == [] and store.get_doc(PROPOSALS, "2026-W40") is None
    only_report = weekly_review(store, None, facts, CONFIG, promo, "test-model", 2500, NOW)
    assert only_report.status == "report_only" and only_report.report == product_lines(facts)


def test_baseline_solo_sopra_soglia():
    facts = {"min_users": 30, "completion": {"window_days": 7, "completion": rate(60, 100, 30), "hints": rate(5, 100, 30)},
             "activation_by_channel": [{"channel": "tiktok", **rate(10, 20, 30)}]}
    assert baseline_and_volume(facts, "daily_completion") == (0.6, 100)
    assert baseline_and_volume(facts, "activation_24h") == (None, 5)  # 20 avvii sotto soglia


def test_configurazioni_versionate():
    product = load_product(ROOT / "config")
    assert product.project_id == "275711" and product.min_users == 30 and product.environment == "production"
    promo = load_promo_facts(ROOT / "config")
    assert promo.languages == ("it", "en", "es") and all(f["source"] for f in promo.facts)
    assert campaign_code("2026-W40", "who_is") == "2026w40-whois"
