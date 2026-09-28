"""Il contratto di un collector: fatti con fonte, momento, cursore e completezza."""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol

from supervisor.core.models import CollectResult


class Collector(Protocol):
    source: str
    kind: str

    def collect(self, cursors: dict[str, Optional[dict]], now: datetime) -> CollectResult:
        """`cursors` contiene il cursore salvato per ogni flusso (None al primo giro)."""
        ...
