"""Messaggi Telegram: chiari, strutturati, sicuri (escape) e sotto il limite."""
import re

from factory import GAME, NOW, PROMO
from supervisor.core.clock import parse_iso
from supervisor.core.models import Finding, SourceReport
from supervisor.reporting.messages import (
    LIMIT,
    brief_message,
    findings_message,
    growth_message,
    pr_opened_message,
    pr_update_message,
)
from supervisor.state.store import snapshot_doc

now = parse_iso(NOW)


def snapshot(*reports):
    return snapshot_doc("r1", NOW, list(reports))


def game(**facts):
    base = {"repo": GAME, "default_branch": "main", "expected_default_branch": "main",
            "workflows": {"ci.yml": {"conclusion": "success"}, "deploy.yml": {"conclusion": "failure"}},
            "open_prs": [{"number": 3, "ci": "failure", "title": "x"}], "open_issues": 2}
    return SourceReport(f"github:{GAME}", "github", True, {**base, **facts})


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def test_brief_senza_problemi_e_breve():
    queue = SourceReport("promo:promo_posts", "promo", True, {"by_status": {"draft": 2, "failed": 0},
                                                              "stale_drafts": [], "published_last_7d": 5})
    text = brief_message(snapshot(game(), queue), [], [], now,
                         budget={"day_actual": 20_000, "day_hard": 1_000_000, "month_actual": 310_000,
                                 "month_hard": 15_000_000, "open_reservations": 0},
                         details="https://github.com/o/r/actions/runs/1")
    lines = plain(text).splitlines()
    assert lines[0] == "☀️ Brief GTP · lun 28 set" and lines[1].startswith("✅ Tutto a posto")
    assert "CI ✅ · Deploy ❌" in text and "PR aperte: 1 (1 con CI rossa) · Issue aperte: 2" in text
    assert "Bozze da approvare: 2 · Fallite: 0" in text and "Pubblicati negli ultimi 7 giorni: 5" in text
    assert "💰 Budget AI: oggi 0,02 $ su 1,00 · mese 0,31 $ su 15,00" in text
    assert '<a href="https://github.com/o/r/actions/runs/1">📄 Report completo</a>' in text
    assert len(plain(text).splitlines()) < 20  # niente muro di testo


def test_brief_con_problemi_in_evidenza():
    findings = [Finding("ci_failed", GAME, "ci.yml:101", "ci.yml fallito su main (run #101)", "alta",
                        ["https://github.com/x/actions/runs/101"], created_at=NOW),
                Finding("promo_drafts_stale", "promo", "drafts", "3 bozze", "bassa", [], created_at="2026-09-20T00:00:00Z")]
    decisions = {findings[0].id: {"state": "proposed", "proposed_action": "Aprire il log della run 101."}}
    text = brief_message(snapshot(game()), findings, [], now, decisions=decisions)
    assert "🔴 <b>2 problemi aperti</b> (1 nuovo)" in text
    first = text.index("CI rossa su main")
    assert first < text.index("Bozze Promo in attesa") < text.index("🎮 Gioco")  # prima i problemi, poi lo stato
    assert "🔴 🆕 <b>CI rossa su main</b> · gioco" in text and "↳ <i>Aprire il log della run 101.</i>" in text


def test_testi_esterni_non_diventano_formattazione():
    evil = Finding("pr_without_green_ci", GAME, "9:abc", "x", "media", ['javascript:alert(1)'], created_at=NOW)
    text = findings_message([evil])
    assert "javascript" not in text and "PR #9 senza CI verde" in text
    opened = pr_opened_message(42, '<b>ciao</b> & <a href="http://x">link</a>', 300, "https://github.com/pr/300")
    assert "&lt;b&gt;ciao&lt;/b&gt; &amp; &lt;a href=&quot;http://x&quot;&gt;" in opened
    assert '<a href="https://github.com/pr/300">Apri la PR #300</a>' in opened


def test_messaggio_lungo_accorciato():
    many = [Finding("pr_without_green_ci", GAME, f"{i}:sha", "x", "bassa", [f"https://github.com/pr/{i}" * 20],
                    created_at=NOW) for i in range(40)]
    text = brief_message(snapshot(game(open_prs=[{"number": i, "ci": "none", "title": "t" * 200} for i in range(50)])),
                         many, [], now)
    assert len(text) <= LIMIT + 100 and "e altri 35" in text


def test_brief_metriche_prodotto():
    product = SourceReport("posthog:275711", "posthog", True, {
        "data_quality": [], "completion": {"completion": {"value": 12 / 14, "numerator": 12, "denominator": 14,
                                                          "status": "sotto_soglia"}},
        "north_star": [], "activation_by_campaign": []})
    text = brief_message(snapshot(game(), product), [], [], now)
    assert "Daily completate (7 giorni): 86% (12 su 14) · pochi dati" in text
    assert "Nuovi giocatori attivati: nessuno da misurare ancora" in text


def test_messaggi_pr_e_growth():
    green = pr_update_message("ci_green", 42, 300, "https://github.com/pr/300", "abcdef1234")
    assert green.startswith("<b>✅ PR pronta per la tua review</b> · gioco #42") and "<code>abcdef1</code>" in green
    assert pr_update_message("ci_failed", 42, 300, "u", "abc").startswith("<b>❌ CI fallita")
    proposal = {"intervention": "Verificare bot_started", "observation": "Mancano ingressi", "kind": "qualitativa",
                "feasibility": {"reason": "sotto soglia"}, "primary_metric": "daily_completion",
                "unverified_numbers": ["73%"]}
    brief = {"campaign_id": "2026w40-whois", "channel": "tiktok", "language": "it", "format": "who_is", "cta": "Gioca"}
    text = growth_message("2026-W40", proposal, brief, "https://github.com/o/r/actions/runs/2")
    assert "<b>Priorità:</b> Verificare bot_started" in text and "🔍 verifica qualitativa" in text
    assert "Daily completate" in text and "⚠️ Numeri da verificare: 73%" in text
    assert "<code>2026w40-whois</code>" in text and "📄 Review completa" in text


def test_nome_breve_dei_repository():
    promo_repo = SourceReport(f"github:{PROMO}", "github", True, {"workflows": {"ci.yml": {"conclusion": "success"},
                                                                                "promo.yml": {"conclusion": "success"}}})
    text = brief_message(snapshot(game(), promo_repo), [], [], now)
    assert "CI ✅ · Cron Promo ✅" in text and "Coda dei post: non collegata" in text
