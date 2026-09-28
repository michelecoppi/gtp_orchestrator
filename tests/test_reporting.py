from factory import GAME, NOW
from supervisor.collectors.http import HttpResponse
from supervisor.core.clock import parse_iso
from supervisor.core.models import Event, Finding, SourceReport
from supervisor.reporting.brief import build_brief, render_findings_alert, render_markdown, render_telegram
from supervisor.reporting.telegram import TelegramNotifier
from supervisor.state.store import MemoryStore, snapshot_doc

now = parse_iso(NOW)
SRC = f"github:{GAME}"


def snapshot(*reports, at=NOW):
    return snapshot_doc("r1", at, list(reports))


def test_senza_snapshot():
    text = render_markdown(build_brief(None, [], [], now))
    assert "Nessuno snapshot" in text


def test_dati_mancanti_sono_non_disponibili_non_zero():
    brief = build_brief(snapshot(SourceReport(SRC, "github", False, {}, ["repo: HTTP 500"]),
                                 SourceReport("promo:promo_posts", "promo", False, configured=False)), [], [], now)
    text = render_markdown(brief)
    assert "PR aperte: non disponibile" in text and "Issue aperte: non disponibile" in text
    assert "Collector non configurato" in text
    assert f"{SRC}: non disponibile — repo: HTTP 500" in text
    assert "promo:promo_posts: non configurata" in text


def test_brief_completo_e_nuovi_finding():
    facts = {"repo": GAME, "default_branch": "claude/x", "expected_default_branch": "main",
             "head": {"sha": "a" * 40, "message": "feat: x", "committed_at": NOW},
             "workflows": {"ci.yml": {"conclusion": "success", "head_sha": "a" * 40}, "deploy.yml": None},
             "open_prs": [{"number": 3, "draft": False, "ci": "failure", "title": "**grassetto** [link](http://x)"}],
             "open_issues": 2}
    old = Finding("ci_failed", GAME, "k1", "vecchio", "alta", created_at="2026-09-20T00:00:00Z")
    new = Finding("pr_without_green_ci", GAME, "k2", "nuovo", "media", ["https://x"], created_at=NOW)
    events = [Event(SRC, "pull_request", "9", "merged", NOW, NOW), Event(SRC, "issue", "8", "closed:completed", NOW, NOW),
              Event(SRC, "pull_request", "7", "merged", "2026-09-20T00:00:00Z", NOW)]  # vecchio, raccolto ora
    brief = build_brief(snapshot(SourceReport(SRC, "github", True, facts)), [old, new], events, now)
    assert brief.new_findings == 1 and brief.open_findings == 2
    text = render_markdown(brief)
    assert "ATTENZIONE: atteso main" in text
    assert "deploy.yml: non disponibile" in text
    assert "NUOVO [media]" in text and "unite 1" in text and "chiuse 1" in text
    assert text.index("[alta]") < text.index("[media]")


def test_snapshot_vecchio_segnalato():
    brief = build_brief(snapshot(at="2026-09-27T20:00:00Z"), [], [], now)
    assert any("snapshot vecchio" in line for line in brief.intro)


def test_telegram_troncato():
    lines = [{"number": i, "draft": False, "ci": "none", "title": "x" * 100} for i in range(100)]
    facts = {"repo": GAME, "open_prs": lines}
    text = render_telegram(build_brief(snapshot(SourceReport(SRC, "github", True, facts)), [], [], now))
    assert len(text) < 4096 and text.endswith("artifact del workflow)")


class FakeTelegram:
    def __init__(self, status=200, ok=True, raises=None):
        self.status, self.ok, self.raises = status, ok, raises
        self.sent = []

    def request(self, method, url, params=None, headers=None, json_body=None):
        if self.raises:
            raise self.raises
        self.sent.append(json_body)
        return HttpResponse(self.status, {"ok": self.ok, "result": {"username": "promo_bot"}})


def test_invio_idempotente_nello_stesso_giorno():
    store, http = MemoryStore(), FakeTelegram()
    notifier = TelegramNotifier(http, "123:abc", "42")
    assert notifier.send(store, "brief", "ciao", now).status == "sent"
    assert notifier.send(store, "brief", "ciao", now).status == "duplicate"
    assert notifier.send(store, "brief", "diverso", now).status == "sent"
    assert len(http.sent) == 2
    assert http.sent[0] == {"chat_id": "42", "text": "ciao", "disable_web_page_preview": True}


def test_invio_fallito_si_ritenta_e_non_espone_il_token():
    store = MemoryStore()
    failing = TelegramNotifier(FakeTelegram(raises=ConnectionError("https://api.telegram.org/bot123:abcdef/sendMessage")),
                               "123:abcdef", "42")
    outcome = failing.send(store, "brief", "ciao", now)
    assert outcome.status == "failed" and "123:abcdef" not in outcome.detail
    retry = TelegramNotifier(FakeTelegram(), "123:abcdef", "42")
    assert retry.send(store, "brief", "ciao", now).status == "sent"


def test_non_configurato():
    assert TelegramNotifier(FakeTelegram(), "", "").send(MemoryStore(), "brief", "x", now).status == "not_configured"


def test_alert_finding():
    text = render_findings_alert([Finding("ci_failed", GAME, "k", "CI rotta", "alta", ["https://x"])])
    assert text.startswith("GTP Supervisor: 1 nuovi finding") and "https://x" in text


def test_sezione_engineering():
    tasks = [
        {"repo": GAME, "issue_number": 42, "state": "awaiting_approval", "phase": "ci_green", "pr_number": 300,
         "pr_url": "https://pr/300", "updated_at": NOW},
        {"repo": GAME, "issue_number": 7, "state": "blocked", "phase": "done", "error": "approvazione revocata",
         "updated_at": NOW},
        {"repo": GAME, "issue_number": 1, "state": "completed", "phase": "done", "updated_at": "2026-09-01T00:00:00Z"},
    ]
    text = render_markdown(build_brief(snapshot(), [], [], now, tasks=tasks))
    assert "guess_the_player_from_the_path#42: CI verde: tocca a te la review — PR #300" in text
    assert "#7: blocked (approvazione revocata)" in text and "#1:" not in text
