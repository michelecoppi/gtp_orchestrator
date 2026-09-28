"""Interfaccia verso i modelli linguistici: solo il contratto, nessun provider in M1.

Da M2 le chiamate reali passeranno da un adapter (candidato: LiteLLM SDK) dietro questa
interfaccia e, prima di ogni invio, dalla prenotazione atomica del budget. Il dominio dipende
solo da `LLMClient`: cambiare libreria o provider non deve toccare regole, stato o report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class LLMRequest:
    task: str
    system: str
    prompt: str
    max_output_tokens: int
    json_schema: Optional[dict] = None


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
    """Risposte preregistrate per task: per test ed evaluation, costo zero."""

    def __init__(self, answers: Optional[dict[str, str]] = None) -> None:
        self.answers = answers or {}
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(self.answers.get(request.task, ""), "fake", "fake-0")
