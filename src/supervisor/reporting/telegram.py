"""Notifiche su Telegram con il bot approvazioni di Promo Studio (docs/adr/0002-bot-condiviso.md).

Il supervisore usa SOLO `sendMessage`: niente pulsanti, niente `getUpdates`. Il comando `sync`
di Promo legge `getUpdates` e ne conferma gli offset; un secondo lettore gli ruberebbe i click
su Approva/Rifiuta. Testo semplice, senza parse_mode e senza anteprime dei link.

Ogni invio e' idempotente: la chiave (contenuto + giorno) si prenota nello stato prima di
chiamare Telegram. Se l'invio fallisce la prenotazione diventa `failed` e si puo' ritentare; se
il processo muore durante l'invio resta `pending` e non si ripete alla cieca.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from supervisor.collectors.http import HttpClient
from supervisor.core.clock import iso, rome_day
from supervisor.core.models import stable_hash
from supervisor.core.scrub import scrub
from supervisor.state.store import StateStore

API = "https://api.telegram.org"


@dataclass
class SendOutcome:
    status: str  # sent | duplicate | failed | not_configured
    detail: str = ""


class TelegramNotifier:
    def __init__(self, http: HttpClient, token: str, chat_id: str) -> None:
        self.http = http
        self.token = token
        self.chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, store: StateStore, kind: str, text: str, now: datetime) -> SendOutcome:
        if not self.configured:
            return SendOutcome("not_configured", "SUP_TELEGRAM_BOT_TOKEN o SUP_ADMIN_CHAT_ID mancanti")
        key = f"{kind}-{rome_day(now)}-{stable_hash(text)[:16]}"
        if not store.claim_notification(key, iso(now), text[:120]):
            return SendOutcome("duplicate", key)
        try:
            response = self.http.request("POST", f"{API}/bot{self.token}/sendMessage", json_body={
                "chat_id": self.chat_id, "text": text, "disable_web_page_preview": True,
            })
            ok = response.status == 200 and isinstance(response.body, dict) and response.body.get("ok")
            detail = "" if ok else f"HTTP {response.status}"
        except Exception as exc:
            ok, detail = False, scrub(f"{type(exc).__name__}: {exc}")[:200]
        store.finish_notification(key, "sent" if ok else "failed", iso(now))
        return SendOutcome("sent", key) if ok else SendOutcome("failed", detail)

    def check(self) -> tuple[bool, str]:
        """Per `doctor`: `getMe` e' in sola lettura e non tocca gli update di Promo."""
        if not self.configured:
            return False, "token o chat id mancanti"
        try:
            response = self.http.request("GET", f"{API}/bot{self.token}/getMe")
        except Exception as exc:
            return False, scrub(f"{type(exc).__name__}: {exc}")[:200]
        if response.status == 200 and isinstance(response.body, dict) and response.body.get("ok"):
            return True, "@" + str((response.body.get("result") or {}).get("username"))
        return False, f"HTTP {response.status}"
