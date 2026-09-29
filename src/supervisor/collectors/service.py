"""Collector del servizio del gioco in produzione: risponde, quale build serve, webhook Telegram sano.

Tutto in sola lettura:
- `GET /` del servizio Cloud Run. Il gioco risponde con `version` e `revision`, dove `revision` e'
  `K_REVISION` (`services/version.py` del gioco): ogni deploy crea una revisione nuova.
- `getWebhookInfo` del bot del gioco, che legge soltanto. Il supervisore non chiama mai
  `setWebhook`, `deleteWebhook` o `getUpdates`: staccherebbero il webhook del gioco.

Il servizio giu' non e' una "sorgente non disponibile": e' proprio il fatto da segnalare. La sorgente
e' incompleta solo quando non si riesce a interrogare Telegram, oppure quando fallisce anche la rete del
runner. Il cursore del flusso `#revision` ricorda da quando e' in servizio la revisione attuale. Con quel
dato la regola `deploy_not_live` riconosce un deploy riuscito che non ha cambiato la revisione.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from supervisor.collectors.http import HttpClient
from supervisor.core.clock import iso
from supervisor.core.config import ServiceConfig
from supervisor.core.models import CollectResult, Event, SourceReport, StreamResult
from supervisor.core.scrub import scrub, untrusted

TELEGRAM = "https://api.telegram.org"
# Cloud Run puo' partire a freddo (anche 10 secondi): un secondo tentativo evita falsi allarmi.
ATTEMPTS = 2


class ServiceCollector:
    kind = "service"

    def __init__(self, config: ServiceConfig, http: HttpClient, bot_token: str = "") -> None:
        self.config = config
        self.http = http
        self.bot_token = bot_token
        self.source = config.source

    def collect(self, cursors: dict[str, Optional[dict]], now: datetime) -> CollectResult:
        stream = f"{self.source}#revision"
        if not self.config.url:
            return CollectResult([], SourceReport(self.source, self.kind, ok=False, configured=False))
        received = iso(now)
        service = self._probe()
        webhook = self._webhook() if self.bot_token else {"configured": False}
        errors = [f"telegram: {webhook['error']}"] if webhook.get("check_failed") else []

        cursor = cursors.get(stream) or {}
        revision = service.get("revision")
        events: list[Event] = []
        if revision and cursor.get("revision") == revision:
            since = cursor.get("since")
        elif revision:
            since = received
            events.append(Event(self.source, "revision", str(revision), "live", received, received,
                                {"version": service.get("version")}))
        else:  # servizio giu' o risposta senza revisione: si tiene quanto gia' noto
            since = cursor.get("since")
        new_cursor = {"revision": revision, "since": since} if revision else (cursor or None)

        facts: dict[str, Any] = {
            "url": self.config.url, "repo": self.config.repo, "deploy_workflow": self.config.deploy_workflow,
            "service": service, "webhook": webhook, "revision": revision, "revision_since": since,
            "webhook_error_hours": self.config.webhook_error_hours,
            "pending_updates_max": self.config.pending_updates_max,
            "deploy_grace_minutes": self.config.deploy_grace_minutes,
        }
        return CollectResult([StreamResult(stream, events, new_cursor)],
                             SourceReport(self.source, self.kind, ok=not errors, facts=facts, errors=errors))

    def _probe(self) -> dict[str, Any]:
        error = ""
        for _ in range(ATTEMPTS):
            try:
                response = self.http.request("GET", self.config.url)
            except Exception as exc:  # timeout, DNS, TLS
                error = scrub(f"{type(exc).__name__}: {exc}")[:200]
                continue
            if response.status == 200:
                body = response.body if isinstance(response.body, dict) else {}
                return {"ok": True, "status": 200, "version": untrusted(body.get("version"), 40) or None,
                        "revision": untrusted(body.get("revision"), 80) or None}
            error = f"HTTP {response.status}"
        return {"ok": False, "error": error}

    def _webhook(self) -> dict[str, Any]:
        try:
            response = self.http.request("GET", f"{TELEGRAM}/bot{self.bot_token}/getWebhookInfo")
        except Exception as exc:
            return {"configured": True, "check_failed": True, "error": scrub(f"{type(exc).__name__}: {exc}")[:200]}
        body = response.body if isinstance(response.body, dict) else {}
        if response.status != 200 or not body.get("ok"):
            return {"configured": True, "check_failed": True, "error": f"HTTP {response.status}"}
        info = body.get("result") or {}
        last_error = info.get("last_error_date")
        return {
            "configured": True,
            "url_set": bool(info.get("url")),
            "pending": int(info.get("pending_update_count") or 0),
            "last_error_at": iso(datetime.fromtimestamp(int(last_error), timezone.utc)) if last_error else None,
            "last_error": untrusted(info.get("last_error_message"), 120) or None,
        }
