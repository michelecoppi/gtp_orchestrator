"""Interfaccia verso i modelli linguistici.

Il dominio dipende solo da `LLMClient`: cambiare libreria o provider non tocca regole, stato o
report. Le chiamate a pagamento passano sempre da `llm/gateway.py` (policy, catalogo, budget), mai
direttamente da un client.

Gli adapter traducono gli errori in due casi che contano per la contabilita':
- `LLMRejected`: il provider ha rifiutato la richiesta (4xx, rate limit): non addebitata;
- `LLMOutcomeUnknown`: timeout, errore di rete o 5xx: potrebbe essere stata addebitata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


class LLMRejected(RuntimeError):
    pass


class LLMOutcomeUnknown(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMRequest:
    model: str
    task: str
    system: str
    prompt: str
    max_output_tokens: int
    json_schema: Optional[dict] = None
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict = field(default_factory=dict)


class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...


class FakeLLM:
    """Risposte preregistrate per task: per test ed evaluation, costo zero.

    `answers[task]` puo' essere un testo o un'eccezione da sollevare."""

    def __init__(self, answers: Optional[dict] = None, input_tokens: int = 1000, output_tokens: int = 100) -> None:
        self.answers = answers or {}
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        answer = self.answers.get(request.task, "")
        if isinstance(answer, BaseException):
            raise answer
        return LLMResponse(str(answer), "fake", request.model, self.input_tokens, self.output_tokens)
