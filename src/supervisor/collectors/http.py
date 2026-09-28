"""Trasporto HTTP sostituibile: `requests` in produzione, fixture registrate in test e replay.

Le fixture sono un file JSON che associa `METODO path?query-ordinata` a una risposta
`{"status": 200, "body": ..., "headers": {...}}`; una chiave senza query vale per qualunque
parametro. Una richiesta senza fixture risponde 404:
nei test una chiamata imprevista diventa un errore visibile, non una chiamata di rete.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol
from urllib.parse import urlencode

DEFAULT_TIMEOUT = 20


@dataclass
class HttpResponse:
    status: int
    body: Any = None
    headers: dict = field(default_factory=dict)


class HttpClient(Protocol):
    def request(self, method: str, url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
                json_body: Optional[dict] = None) -> HttpResponse: ...


class RequestsHttp:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        import requests

        self._session = requests.Session()
        self.timeout = timeout

    def request(self, method, url, params=None, headers=None, json_body=None):
        response = self._session.request(method, url, params=params, headers=headers, json=json_body,
                                         timeout=self.timeout)
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = response.text[:500]
        return HttpResponse(response.status_code, body, {k.lower(): v for k, v in response.headers.items()})


def fixture_key(method: str, url: str, params: Optional[dict] = None) -> str:
    path = url.split("://", 1)[-1]
    path = path[path.find("/"):] if "/" in path else "/"
    query = urlencode(sorted((params or {}).items()))
    return f"{method.upper()} {path}" + (f"?{query}" if query else "")


class FixtureHttp:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.bodies: list[tuple[str, Optional[dict]]] = []

    @classmethod
    def from_file(cls, path: Path | str) -> FixtureHttp:
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def request(self, method, url, params=None, headers=None, json_body=None):
        key = fixture_key(method, url, params)
        self.calls.append(key)
        self.bodies.append((key, json_body))
        # Una fixture senza query vale per qualunque parametro (per esempio `since`, che cambia a
        # ogni giro): le risposte ripetute le scarta la deduplicazione degli eventi.
        item = self.responses.get(key) or self.responses.get(key.split("?", 1)[0])
        if item is None:
            return HttpResponse(404, {"message": f"nessuna fixture per {key}"})
        wanted_etag = (headers or {}).get("If-None-Match")
        response_headers = {k.lower(): v for k, v in (item.get("headers") or {}).items()}
        if wanted_etag and wanted_etag == response_headers.get("etag"):
            return HttpResponse(304, None, response_headers)
        return HttpResponse(item.get("status", 200), item.get("body"), response_headers)
