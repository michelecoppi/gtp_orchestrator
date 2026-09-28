"""Testo senza segreti: si applica a log, errori e a tutto cio' che finisce in un report."""
import re

_BOT_URL_TOKEN = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_BEARER = re.compile(r"((?:Bearer|token)\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_GH_TOKEN = re.compile(r"\b(?:gh[opsu]_|github_pat_)[A-Za-z0-9_]{20,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
REDACTED = "[REDACTED]"

_secrets: list[str] = []


def register_secrets(*values: str | None) -> None:
    for value in values:
        if value and len(value) >= 6 and value not in _secrets:
            _secrets.append(value)


def scrub(text: object) -> str:
    result = str(text)
    for value in _secrets:
        result = result.replace(value, REDACTED)
    result = _BOT_URL_TOKEN.sub("/bot" + REDACTED, result)
    result = _BEARER.sub(r"\1" + REDACTED, result)
    return _GH_TOKEN.sub(REDACTED, result)


def untrusted(text: object, limit: int = 120) -> str:
    """Testo preso da issue, PR, log o caption: dato non fidato.

    Si tiene su una riga, senza caratteri di controllo e troncato. Non viene mai interpretato:
    finisce nei report solo come citazione (Telegram lo riceve senza parse_mode)."""
    one_line = " ".join(_CONTROL.sub(" ", str(text or "")).split())
    one_line = scrub(one_line)
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"
