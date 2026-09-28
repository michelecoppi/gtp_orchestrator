"""Tempo: i dati si conservano in UTC, i report si leggono in Europe/Rome.

Le date sono stringhe ISO 8601 in UTC (`2026-09-28T10:00:00Z`), come in Promo Studio:
ordinabili come stringhe e identiche in SQLite e su Firestore.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def rome_day(moment: datetime) -> str:
    return moment.astimezone(ROME).date().isoformat()


def rome_label(moment: datetime) -> str:
    return moment.astimezone(ROME).strftime("%d/%m/%Y %H:%M")


def hours_between(start: str, end: datetime) -> float:
    return (end - parse_iso(start)) / timedelta(hours=1)
