"""Estrazione del JSON dalle risposte dei modelli.

Non tutti i provider rispettano lo schema "strict" via OpenRouter: alcuni aggiungono una frase prima del
JSON o lo racchiudono in un blocco ```json. Si accetta il primo oggetto JSON completo del testo; tipi e
campi si validano comunque dopo, con lo schema.
"""
from __future__ import annotations

import json
from typing import Any

_DECODER = json.JSONDecoder()


class NoJsonObject(ValueError):
    pass


def extract_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError as exc:
        first_error = exc.msg
    else:
        first_error = "non e' un oggetto"
    start = raw.find("{")
    while start != -1:
        try:
            data, _ = _DECODER.raw_decode(raw, start)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        start = raw.find("{", start + 1)
    raise NoJsonObject(first_error)
