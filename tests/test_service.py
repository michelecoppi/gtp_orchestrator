from factory import GAME
from supervisor.collectors.http import HttpResponse
from supervisor.collectors.service import ServiceCollector
from supervisor.core.clock import parse_iso
from supervisor.core.config import ServiceConfig, Sources
from supervisor.core.models import SourceReport
from supervisor.core.scrub import register_secrets
from supervisor.reporting.messages import _service_line
from supervisor.rules.engine import evaluate

URL = "https://game.example.run.app/"
TOKEN = "123456:game-bot-token"
CONFIG = ServiceConfig(url=URL, repo=GAME)
NOW = "2026-09-29T12:00:00Z"
now = parse_iso(NOW)


class FakeHttp:
    """Risposte in ordine per URL; un'eccezione nella lista viene sollevata."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def request(self, method, url, params=None, headers=None, json_body=None):
        self.calls.append((method, url))
        key = "telegram" if "api.telegram.org" in url else "service"
        answer = self.routes[key].pop(0) if len(self.routes[key]) > 1 else self.routes[key][0]
        if isinstance(answer, Exception):
            raise answer
        return answer


ALIVE = HttpResponse(200, {"message": "Bot attivo!", "version": "0.1.0", "revision": "guess-the-player-00164"})
HOOK_OK = HttpResponse(200, {"ok": True, "result": {"url": "https://x/webhook", "pending_update_count": 0}})


def collect(routes, cursor=None, token=TOKEN, at=NOW):
    http = FakeHttp(routes)
    result = ServiceCollector(CONFIG, http, token).collect({"service:game#revision": cursor}, parse_iso(at))
    return result, http


def rules(report, deploy=None, at=NOW):
    github = SourceReport(f"github:{GAME}", "github", True, {"workflows": {"deploy.yml": deploy} if deploy else {}})
    return evaluate([], [report, github], [], Sources(github=(), service=CONFIG), parse_iso(at), "r1")


def test_servizio_vivo_e_prima_revisione():
    result, http = collect({"service": [ALIVE], "telegram": [HOOK_OK]})
    facts = result.report.facts
    assert result.report.ok and facts["service"]["ok"] and facts["revision"] == "guess-the-player-00164"
    assert facts["revision_since"] == NOW and facts["webhook"]["url_set"] is True
    assert [e.type for e in result.streams[0].events] == ["revision"]
    assert result.streams[0].cursor == {"revision": "guess-the-player-00164", "since": NOW}
    assert rules(result.report).findings == []


def test_revisione_gia_nota_conserva_la_data():
    cursor = {"revision": "guess-the-player-00164", "since": "2026-09-28T08:00:00Z"}
    result, _ = collect({"service": [ALIVE], "telegram": [HOOK_OK]}, cursor)
    assert result.report.facts["revision_since"] == "2026-09-28T08:00:00Z"
    assert result.streams[0].events == []


def test_avvio_a_freddo_si_ritenta_una_volta():
    result, http = collect({"service": [TimeoutError("read timed out"), ALIVE], "telegram": [HOOK_OK]})
    assert result.report.facts["service"]["ok"]
    assert sum(1 for _, url in http.calls if url == URL) == 2


def test_gioco_giu():
    result, _ = collect({"service": [HttpResponse(503, "Service Unavailable")], "telegram": [HOOK_OK]},
                        {"revision": "r1", "since": "2026-09-28T08:00:00Z"})
    facts = result.report.facts
    assert result.report.ok, "il servizio giu' e' un fatto, non una sorgente mancante"
    assert facts["service"] == {"ok": False, "error": "HTTP 503"}
    assert result.streams[0].cursor == {"revision": "r1", "since": "2026-09-28T08:00:00Z"}
    out = rules(result.report)
    assert [(f.rule, f.severity) for f in out.findings] == [("game_down", "alta")]


def test_webhook_mancante_o_con_errori():
    no_hook = HttpResponse(200, {"ok": True, "result": {"url": "", "pending_update_count": 0}})
    result, _ = collect({"service": [ALIVE], "telegram": [no_hook]})
    assert [f.rule for f in rules(result.report).findings] == ["webhook_missing"]

    errors = HttpResponse(200, {"ok": True, "result": {
        "url": "https://x/webhook", "pending_update_count": 3,
        "last_error_date": int(parse_iso("2026-09-29T11:30:00Z").timestamp()),
        "last_error_message": "Wrong response from the webhook: 500 Internal Server Error"}})
    result, _ = collect({"service": [ALIVE], "telegram": [errors]})
    found = rules(result.report).findings
    assert [(f.rule, f.severity) for f in found] == [("webhook_errors", "media")]
    assert "500 Internal Server Error" in found[0].statement

    # Un errore vecchio non e' un problema attuale.
    old = HttpResponse(200, {"ok": True, "result": {
        "url": "https://x/webhook", "pending_update_count": 0,
        "last_error_date": int(parse_iso("2026-09-28T11:30:00Z").timestamp()), "last_error_message": "x"}})
    result, _ = collect({"service": [ALIVE], "telegram": [old]})
    assert rules(result.report).findings == []

    # Coda che cresce: severita' alta anche senza errori.
    queue = HttpResponse(200, {"ok": True, "result": {"url": "https://x/webhook", "pending_update_count": 80}})
    result, _ = collect({"service": [ALIVE], "telegram": [queue]})
    assert [(f.rule, f.severity) for f in rules(result.report).findings] == [("webhook_errors", "alta")]


def test_telegram_irraggiungibile_rende_la_sorgente_incompleta_senza_esporre_il_token():
    register_secrets(TOKEN)
    boom = ConnectionError(f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo failed")
    result, _ = collect({"service": [ALIVE], "telegram": [boom]})
    assert not result.report.ok and TOKEN not in " ".join(result.report.errors)
    assert [f.rule for f in rules(result.report).findings] == ["source_unavailable"]


def test_senza_token_il_webhook_non_si_controlla():
    result, http = collect({"service": [ALIVE], "telegram": [HOOK_OK]}, token="")
    assert result.report.facts["webhook"] == {"configured": False}
    assert all("telegram" not in url for _, url in http.calls)


def test_deploy_riuscito_ma_revisione_vecchia():
    cursor = {"revision": "guess-the-player-00164", "since": "2026-09-28T08:00:00Z"}
    result, _ = collect({"service": [ALIVE], "telegram": [HOOK_OK]}, cursor)
    deploy = {"id": 77, "conclusion": "success", "head_sha": "abcdef1234", "url": "https://run/77",
              "at": "2026-09-29T10:00:00Z"}
    found = rules(result.report, deploy).findings
    assert [(f.rule, f.key) for f in found] == [("deploy_not_live", "77")]
    # Entro il margine non si segnala ancora.
    assert rules(result.report, {**deploy, "at": "2026-09-29T11:45:00Z"}).findings == []
    # Deploy precedente all'arrivo della revisione: tutto regolare.
    assert rules(result.report, {**deploy, "at": "2026-09-28T07:55:00Z"}).findings == []


def test_riga_del_brief():
    result, _ = collect({"service": [ALIVE], "telegram": [HOOK_OK]})
    assert _service_line(result.report) == "In produzione: ✅ guess-the-player-00164 · webhook ✅"
    down, _ = collect({"service": [HttpResponse(503, None)], "telegram": [HOOK_OK]})
    assert _service_line(down.report) == "In produzione: ❌ non risponde (HTTP 503)"
