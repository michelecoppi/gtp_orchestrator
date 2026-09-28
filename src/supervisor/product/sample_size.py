"""Numerosita' per un confronto fra due proporzioni (specifica, sez. 11).

Con pochi utenti non si chiama "vincitore" una variante: prima di proporre un esperimento si calcola quanti
utenti servono per vedere il miglioramento minimo rilevante, e in quante settimane arriverebbero con il
volume attuale. Se non e' raggiungibile, meglio verifiche del funnel e feedback qualitativo.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Optional


@dataclass(frozen=True)
class Feasibility:
    baseline: Optional[float]
    target: Optional[float]
    per_arm: Optional[int]
    weekly_eligible: int
    weeks: Optional[int]
    feasible: bool
    reason: str


def per_arm(baseline: float, target: float, alpha: float = 0.05, power: float = 0.80) -> int:
    """Utenti per braccio (test bilaterale, due proporzioni indipendenti, allocazione 1:1)."""
    if not (0 < baseline < 1 and 0 < target < 1) or baseline == target:
        raise ValueError("proporzioni non valide")
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_beta = NormalDist().inv_cdf(power)
    pooled = (baseline + target) / 2
    numerator = (z_alpha * math.sqrt(2 * pooled * (1 - pooled))
                 + z_beta * math.sqrt(baseline * (1 - baseline) + target * (1 - target))) ** 2
    return math.ceil(numerator / (target - baseline) ** 2)


def feasibility(baseline: Optional[float], relative_lift: float, weekly_eligible: int, *, alpha: float = 0.05,
                power: float = 0.80, max_weeks: int = 8) -> Feasibility:
    if baseline is None:
        return Feasibility(None, None, None, weekly_eligible, None, False,
                           "tasso di partenza non disponibile o sotto soglia: nessun esperimento misurabile")
    target = min(0.99, baseline * (1 + relative_lift))
    if not 0 < baseline < 1 or target <= baseline:
        return Feasibility(baseline, target, None, weekly_eligible, None, False,
                           "tasso di partenza al limite (0% o 100%): un confronto non e' informativo")
    n = per_arm(baseline, target, alpha, power)
    if weekly_eligible <= 0:
        return Feasibility(baseline, target, n, weekly_eligible, None, False,
                           "nessun utente idoneo a settimana")
    weeks = math.ceil(2 * n / weekly_eligible)
    if weeks > max_weeks:
        return Feasibility(baseline, target, n, weekly_eligible, weeks, False,
                           f"servono {2 * n} utenti ({weeks} settimane al volume attuale), oltre il limite di "
                           f"{max_weeks}: preferire verifiche del funnel e feedback qualitativo")
    return Feasibility(baseline, target, n, weekly_eligible, weeks, True,
                       f"{n} utenti per braccio, circa {weeks} settimane al volume attuale")
