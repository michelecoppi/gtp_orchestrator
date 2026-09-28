"""Adapter LiteLLM SDK (requirements-ai.txt), senza gateway separato.

Un solo responsabile dei tentativi: qui zero retry (`num_retries=0`, `max_retries=0`), nessun
fallback del router. Il gateway decide se e quando riprovare, con una nuova prenotazione.
Le chiavi arrivano dall'ambiente con i nomi standard (OPENAI_API_KEY, ANTHROPIC_API_KEY,
GEMINI_API_KEY): credenziali dedicate al supervisore, non condivise con altri progetti.
"""
from __future__ import annotations

from typing import Any

from supervisor.core.scrub import scrub
from supervisor.llm.client import LLMOutcomeUnknown, LLMRejected, LLMRequest, LLMResponse

# Rifiuti del provider prima dell'elaborazione: nessun addebito. 402 = credito insufficiente (OpenRouter).
NOT_BILLED_STATUS = {400, 401, 402, 403, 404, 413, 422, 429}


class LiteLLMClient:
    def __init__(self, completion: Any = None) -> None:
        if completion is None:
            import litellm

            litellm.telemetry = False
            completion = litellm.completion
        self._completion = completion

    def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.prompt}],
            "max_tokens": request.max_output_tokens,
            "timeout": request.timeout_seconds,
            "num_retries": 0,
            "max_retries": 0,
        }
        if request.reasoning_effort:
            kwargs["reasoning_effort"] = request.reasoning_effort
        if request.json_schema:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": request.task, "schema": request.json_schema, "strict": True}}
        try:
            response = self._completion(**kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            message = scrub(f"{type(exc).__name__}: {exc}")[:300]
            # OpenRouter puo' incapsulare il codice nel corpo di un APIError generico.
            if status in NOT_BILLED_STATUS or any(f'"code":{c}' in str(exc) for c in (402, 429)):
                raise LLMRejected(message) from exc
            raise LLMOutcomeUnknown(message) from exc
        usage = getattr(response, "usage", None)
        choice = response.choices[0]
        return LLMResponse(
            text=(choice.message.content or ""),
            provider=request.model.split("/", 1)[0],
            model=getattr(response, "model", request.model) or request.model,
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )
