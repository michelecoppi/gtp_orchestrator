import pytest

from ai_factory import gateway, write_ai_config
from supervisor.core.clock import parse_iso
from supervisor.core.policy import load_policy
from supervisor.llm.catalog import cost_micros, load_catalog, load_routing
from supervisor.llm.client import FakeLLM, LLMOutcomeUnknown, LLMRejected
from supervisor.llm.gateway import LLMBlocked, LLMCallFailed
from supervisor.llm.litellm_client import LiteLLMClient
from supervisor.state.store import MemoryStore

NOW = parse_iso("2026-09-28T08:00:00Z")


def call(gw, task_id="t1", prompt="ciao", max_out=500, **kw):
    return gw.call(task_id=task_id, task="triage", model_key="test-model", system="sistema", prompt=prompt,
                   max_output_tokens=max_out, now=NOW, **kw)


def test_chiamata_riuscita_addebita_il_costo_reale(tmp_path):
    store, llm = MemoryStore(), FakeLLM({"triage": "ok"}, input_tokens=40, output_tokens=100)
    gw = gateway(store, write_ai_config(tmp_path), llm)
    result = call(gw)
    assert result.cost_micros == 40 * 1 + 100 * 2  # 1 e 2 USD per milione di token
    assert llm.requests[0].model == "openai/test-model" and llm.requests[0].max_output_tokens == 500
    usage = store.get_doc("usage", result.call_id)
    assert usage["state"] == "settled" and usage["reserved_micros"] > usage["actual_micros"]
    s = gw.ledger.summary(NOW)
    assert s["day_actual"] == 240 and s["day_reserved"] == 0


@pytest.mark.parametrize("kwargs,config,reason", [
    ({"paid": "deny"}, {}, "policy"),
    ({"ai_enabled": False}, {}, "SUP_AI_ENABLED"),
    ({}, {"approved": False}, "non approvato"),
    ({}, {"verified": False}, "non verificato"),
    ({}, {"enabled": False}, "disabilitato"),
    ({}, {"price_until": "2026-09-27"}, "nessun prezzo valido"),
    ({}, {"daily_hard": 0.001}, "tetto giornaliero"),
])
def test_bloccata_prima_dell_invio(tmp_path, kwargs, config, reason):
    store, llm = MemoryStore(), FakeLLM({"triage": "ok"})
    gw = gateway(store, write_ai_config(tmp_path, **config), llm, **kwargs)
    with pytest.raises(LLMBlocked, match=reason):
        call(gw)
    assert llm.requests == [] and store.query_docs("usage", "task_id", "t1") == []


def test_input_troppo_lungo_bloccato_senza_fermare_gli_altri(tmp_path):
    gw = gateway(MemoryStore(), write_ai_config(tmp_path), FakeLLM())
    with pytest.raises(LLMBlocked, match="oltre il limite") as exc:
        call(gw, prompt="x" * 10_000)
    assert exc.value.systemic is False


def test_rifiuto_del_provider_libera_la_prenotazione(tmp_path):
    store = MemoryStore()
    gw = gateway(store, write_ai_config(tmp_path), FakeLLM({"triage": LLMRejected("429 rate limit")}))
    with pytest.raises(LLMCallFailed) as exc:
        call(gw)
    assert not exc.value.needs_reconcile
    assert store.get_doc("usage", "t1#1")["state"] == "released"
    assert gw.ledger.summary(NOW)["day_reserved"] == 0


def test_timeout_mantiene_la_prenotazione_e_blocca_il_task(tmp_path):
    store = MemoryStore()
    llm = FakeLLM({"triage": LLMOutcomeUnknown("Timeout")})
    gw = gateway(store, write_ai_config(tmp_path), llm)
    with pytest.raises(LLMCallFailed) as exc:
        call(gw)
    assert exc.value.needs_reconcile
    reserved = gw.ledger.summary(NOW)["day_reserved"]
    assert reserved > 0 and gw.ledger.open_reservations()[0]["call_id"] == "t1#1"
    llm.answers["triage"] = "ok"
    with pytest.raises(LLMBlocked, match="esito incerto"):
        call(gw)  # nessuna ripetizione alla cieca
    assert len(llm.requests) == 1
    gw.ledger.release("t1#1", NOW, "verificato sulla dashboard")
    assert call(gw).call_id == "t1#2"


def test_errore_inatteso_del_client_e_esito_incerto(tmp_path):
    gw = gateway(MemoryStore(), write_ai_config(tmp_path), FakeLLM({"triage": ValueError("boh")}))
    with pytest.raises(LLMCallFailed) as exc:
        call(gw)
    assert exc.value.needs_reconcile


def test_usage_assente_addebita_l_intera_prenotazione(tmp_path):
    store = MemoryStore()
    gw = gateway(store, write_ai_config(tmp_path), FakeLLM({"triage": "ok"}, input_tokens=0, output_tokens=0))
    result = call(gw)
    usage = store.get_doc("usage", result.call_id)
    assert usage["actual_micros"] == usage["reserved_micros"] and "usage assente" in usage["note"]


def test_limite_di_chiamate_per_task(tmp_path):
    gw = gateway(MemoryStore(), write_ai_config(tmp_path, max_calls=2), FakeLLM({"triage": "ok"}))
    call(gw)
    call(gw)
    with pytest.raises(LLMBlocked, match="limite di 2"):
        call(gw)


class BrokenStore(MemoryStore):
    def transact(self, refs, fn):
        raise ConnectionError("Firestore irraggiungibile")

    def get_doc(self, collection, doc_id):
        raise ConnectionError("Firestore irraggiungibile")


def test_stato_non_disponibile_blocca_le_chiamate(tmp_path):
    llm = FakeLLM({"triage": "ok"})
    gw = gateway(BrokenStore(), write_ai_config(tmp_path), llm)
    with pytest.raises(LLMBlocked, match="non disponibile"):
        call(gw)
    assert llm.requests == []


def test_prezzo_promozionale_gemini_e_listino_2027():
    catalog = load_catalog("config")
    gemini = catalog.get("gemini-3.8-flash")
    assert gemini.price_on(parse_iso("2026-12-31T12:00:00Z").date()).input_usd_per_mtok == 0.75
    assert gemini.price_on(parse_iso("2027-01-01T12:00:00Z").date()).input_usd_per_mtok == 1.50
    assert cost_micros(gemini.prices[0], 100_000, 10_000) == 112_500  # 0,075 + 0,0375 USD


def test_catalogo_routing_e_policy_versionati():
    catalog = load_catalog("config")
    assert all(not m.access_verified for m in catalog.models.values())  # nessun accesso dato per scontato
    assert not catalog.get("gpt-6-astra").enabled
    route = load_routing("config")["triage"]
    assert catalog.get(route.model) is not None
    policy = load_policy()
    assert policy.decide("call_paid_llm") == "budget"
    assert policy.decide("merge_or_deploy") == "human" and policy.decide("azione_sconosciuta") == "deny"


class FakeProviderError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status} con chiave sk-segreta123456")
        self.status_code = status


def test_adapter_litellm_traduce_gli_errori_senza_retry():
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        raise FakeProviderError(seen.get("_status", 429))

    from supervisor.core.scrub import register_secrets
    from supervisor.llm.client import LLMRequest

    register_secrets("sk-segreta123456")
    request = LLMRequest(model="openai/x", task="triage", system="s", prompt="p", max_output_tokens=10,
                         json_schema={"type": "object"})
    with pytest.raises(LLMRejected) as rejected:
        LiteLLMClient(completion).complete(request)
    assert "sk-segreta" not in str(rejected.value)
    assert seen["num_retries"] == 0 and seen["max_retries"] == 0 and seen["max_tokens"] == 10
    assert seen["response_format"]["json_schema"]["strict"] is True

    def completion_503(**kwargs):
        raise FakeProviderError(503)

    with pytest.raises(LLMOutcomeUnknown):
        LiteLLMClient(completion_503).complete(request)


def test_adapter_litellm_legge_usage():
    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def completion(**kwargs):
        return Obj(model="gpt-6-luna-2026", usage=Obj(prompt_tokens=12, completion_tokens=3),
                   choices=[Obj(message=Obj(content="ok"))])

    from supervisor.llm.client import LLMRequest

    r = LiteLLMClient(completion).complete(LLMRequest("openai/gpt-6-luna", "t", "s", "p", 5))
    assert (r.text, r.input_tokens, r.output_tokens, r.provider) == ("ok", 12, 3, "openai")
