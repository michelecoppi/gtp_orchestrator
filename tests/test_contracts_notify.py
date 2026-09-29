"""Avviso su Telegram quando un contratto con Promo va riallineato (issue #14), senza rete.

GitHub risponde con `FixtureHttp` (le stesse risposte di test_promo_contract.py), Telegram con un finto che
registra i messaggi, lo stato e' `MemoryStore`. Si controlla che un cambiamento produca un solo messaggio
chiaro, anche dopo la potatura delle notifiche, e che "non verificabile" non mandi nulla.
"""
import json
from datetime import timedelta

import pytest

from supervisor import contracts
from supervisor.cli import main
from supervisor.collectors.http import FixtureHttp, HttpResponse
from supervisor.core.clock import parse_iso
from supervisor.reporting.telegram import TelegramNotifier
from supervisor.state.store import NOTIFICATIONS, MemoryStore
from test_promo_contract import CONTRACTS, github

NOW = parse_iso("2026-10-05T06:30:00Z")
RUN = "https://github.com/michelecoppi/gtp_orchestrator/actions/runs/1"


class FakeTelegram:
    def __init__(self, status=200, raises=None):
        self.status, self.raises, self.sent = status, raises, []

    def request(self, method, url, params=None, headers=None, json_body=None):
        if self.raises:
            raise self.raises
        self.sent.append(json_body)
        return HttpResponse(self.status, {"ok": self.status == 200})


@pytest.fixture
def copy_dir(tmp_path):
    for name in ("promo_post.v1.json", "promo_post.v1.lock.json"):
        (tmp_path / name).write_bytes((CONTRACTS / name).read_bytes())
    return tmp_path


def changed_schema(copy_dir, drop="created_for") -> bytes:
    schema = json.loads((copy_dir / "promo_post.v1.json").read_bytes())
    schema["required"].remove(drop)
    return json.dumps(schema, indent=2).encode()


def checked(copy_dir, content=None, commit="d" * 40, names=("promo_post.v1.json",)):
    content = content if content is not None else (copy_dir / "promo_post.v1.json").read_bytes()
    return contracts.check(github(content, commit, names), directory=copy_dir)


def send(results, store, http):
    return contracts.notify(results, store, TelegramNotifier(http, "123:abc", "42"), NOW, RUN)


def test_uno_schema_cambiato_manda_un_messaggio_chiaro(copy_dir):
    result = checked(copy_dir, changed_schema(copy_dir))
    assert result.state == "changed" and result.drift
    store, http = MemoryStore(), FakeTelegram()
    ok, lines = send([result], store, http)
    assert ok and lines == [f"promo_post.v1: avviso mandato su Telegram ({contracts.alert_key(result)})"]
    (message,) = http.sent
    assert message["parse_mode"] == "HTML" and message["chat_id"] == "42"
    text = message["text"]
    lock = contracts.load_lock(directory=copy_dir)
    assert text.startswith("⚠️ <b>Contratto con Promo da riallineare</b> · <code>promo_post.v1</code>")
    assert "<code>docs/schemas/promo_post.v1.json</code> su main di Promo" in text
    assert f"SHA su Promo: <code>{'d' * 12}</code>" in text
    assert f"SHA della copia: <code>{lock['commit'][:12]}</code>" in text
    assert f'href="https://github.com/michelecoppi/promo_studio/commit/{lock["commit"]}"' in text
    assert "<code>python -m supervisor contracts check --update</code>" in text
    assert "POST_FIELDS e HISTORY_FIELDS" in text and f'href="{RUN}"' in text


def test_lo_stesso_cambiamento_si_segnala_una_volta_sola(copy_dir):
    store, http = MemoryStore(), FakeTelegram()
    send([checked(copy_dir, changed_schema(copy_dir))], store, http)
    # Il lunedi' dopo: stesso schema su Promo, copia non ancora riallineata.
    ok, lines = send([checked(copy_dir, changed_schema(copy_dir), commit="e" * 40)], store, http)
    assert ok and "gia' segnalato" in lines[0] and len(http.sent) == 1
    # Anche dopo la potatura di `notifications` (30 giorni) l'avviso non torna.
    key = contracts.alert_key(checked(copy_dir, changed_schema(copy_dir)))
    assert store.prune_expired(NOW + timedelta(days=31))[NOTIFICATIONS] == 1
    assert store.get_doc(NOTIFICATIONS, key) is None
    send([checked(copy_dir, changed_schema(copy_dir))], store, http)
    assert len(http.sent) == 1
    # Un cambiamento diverso, invece, si segnala.
    send([checked(copy_dir, changed_schema(copy_dir, drop="attempts"))], store, http)
    assert len(http.sent) == 2


def test_versione_nuova_segnalata_anche_se_la_v1_non_cambia(copy_dir):
    lock = contracts.load_lock(directory=copy_dir)
    result = checked(copy_dir, commit=lock["commit"], names=("promo_post.v1.json", "promo_post.v2.json"))
    assert result.state == "aligned" and result.drift
    http = FakeTelegram()
    send([result], MemoryStore(), http)
    (message,) = http.sent
    assert "Versione nuova in Promo: <code>promo_post.v2.json</code>" in message["text"]
    assert "non coincide più" not in message["text"] and "serve anche un'issue" in message["text"]


def test_non_verificabile_e_allineato_non_mandano_nulla(copy_dir):
    offline = contracts.check(FixtureHttp({}), directory=copy_dir)  # 404: nessuna fixture
    assert offline.state == "unverifiable" and not offline.drift
    lock = contracts.load_lock(directory=copy_dir)
    aligned = checked(copy_dir, commit=lock["commit"])
    http = FakeTelegram()
    ok, lines = send([offline, aligned], MemoryStore(), http)
    assert ok and http.sent == []
    assert lines == ["promo_post.v1: non verificabile, nessun avviso su Telegram (solo nel riepilogo)"]
    assert send([aligned], MemoryStore(), http)[1] == ["contratti allineati: nessun avviso"]


def test_invio_fallito_si_ritenta_al_giro_dopo(copy_dir):
    result = checked(copy_dir, changed_schema(copy_dir))
    store = MemoryStore()
    down = ConnectionError("https://api.telegram.org/bot123:abc/sendMessage")
    ok, lines = send([result], store, FakeTelegram(raises=down))
    assert not ok and "avviso non mandato (failed" in lines[0] and "123:abc" not in lines[0]
    assert store.get_doc(contracts.CONTRACT_ALERTS, contracts.alert_key(result)) is None
    http = FakeTelegram()
    assert send([result], store, http)[0] and len(http.sent) == 1
    assert store.get_doc(contracts.CONTRACT_ALERTS, contracts.alert_key(result))["remote_commit"] == "d" * 40


def test_testi_dinamici_con_escape(copy_dir):
    result = checked(copy_dir, changed_schema(copy_dir))
    result.name, result.ref = "<b>x</b>", "main&<i>"
    text = contracts.alert_message(result)
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "main&amp;&lt;i&gt;" in text


def test_report_da_check_a_notify(copy_dir, tmp_path):
    result = checked(copy_dir, changed_schema(copy_dir))
    path = tmp_path / "out" / "contracts.json"
    contracts.write_report([result], path, NOW)
    (back,) = contracts.read_report(path)
    assert back == result and back.drift


def test_comando_notify(copy_dir, tmp_path, monkeypatch, capsys):
    for var in ("SUP_TELEGRAM_BOT_TOKEN", "SUP_ADMIN_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SUP_STORE", "memory")
    path = tmp_path / "contracts.json"
    assert main(["contracts", "notify", "--report", str(path)]) == 0
    assert "niente da segnalare" in capsys.readouterr().out
    contracts.write_report([checked(copy_dir, changed_schema(copy_dir))], path, NOW)
    monkeypatch.setenv("SUP_ENABLED", "false")
    assert main(["contracts", "notify", "--report", str(path)]) == 0
    assert "SUP_ENABLED=false" in capsys.readouterr().out
    monkeypatch.setenv("SUP_ENABLED", "true")
    assert main(["contracts", "notify", "--report", str(path)]) == 0  # senza bot: nessuna rete, nessun errore
    assert "avviso non mandato (not_configured" in capsys.readouterr().out
