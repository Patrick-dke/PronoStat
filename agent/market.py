"""Module d'analyse du marché des cotes.

Responsabilité unique : lire ce que disent les bookmakers — leur consensus,
leur dispersion, et la façon dont leurs cotes ont bougé. C'est le seul module
qui interprète le marché ; les autres se contentent de consommer son verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import config as cfg
from data_sources import Bundle
from engine import map_h2h_odds, remove_vig


@dataclass
class MarketRead:
    """Lecture du marché pour un match."""

    available: bool = False
    probabilities: dict[str, float] = field(default_factory=dict)  # no-vig
    margin: float | None = None            # marge du bookmaker
    bookmakers: int = 0
    dispersion: float | None = None        # désaccord entre bookmakers
    drift: dict[str, float] = field(default_factory=dict)
    drift_hours: float | None = None
    favourite: str | None = None           # "home" | "away"
    notes: list[str] = field(default_factory=list)
    unavailable_reason: str | None = None  # pourquoi il n'y a pas de cotes
    unavailable_hint: str | None = None    # ce que l'utilisateur peut y faire
    reference: dict[str, float] | None = None   # repère tiré de la saison
    reference_detail: str = ""

    @property
    def consensus_strength(self) -> float:
        """Netteté du consensus : 0 = match indécis, 1 = favori écrasant."""
        if not self.probabilities:
            return 0.0
        home = self.probabilities.get("home", 0.0)
        away = self.probabilities.get("away", 0.0)
        return min(1.0, abs(home - away) / 0.6)

    @property
    def agreement(self) -> float:
        """Accord entre bookmakers : 1 = unanimes, 0 = très dispersés."""
        if self.dispersion is None:
            return 0.5 if self.available else 0.0
        return max(0.0, 1.0 - self.dispersion / 0.15)

    def strongest_drift(self) -> tuple[str, float] | None:
        if not self.drift:
            return None
        return max(self.drift.items(), key=lambda kv: abs(kv[1]))


class MarketAnalyst:
    """Extrait du marché tout ce qui est exploitable, sans rien y ajouter."""

    def read(self, bundle: Bundle) -> MarketRead:
        odds = bundle.odds
        if odds is None or not odds.has_h2h:
            # On rapporte la cause exacte relevée pendant la collecte plutôt
            # qu'un « indisponible » sans explication.
            diagnostics = bundle.odds_diagnostics
            read = MarketRead(available=False, notes=[diagnostics.reason])
            read.unavailable_reason = diagnostics.reason
            read.unavailable_hint = diagnostics.actionable_hint
            # Repère de saison, s'il a pu être calculé : c'est un ancrage
            # utile, mais il ne remplace pas une cote du match.
            read.reference = bundle.market_reference
            read.reference_detail = bundle.market_reference_detail
            return read

        mapped = map_h2h_odds(odds.h2h, bundle.home, bundle.away)
        if not mapped:
            return MarketRead(
                available=False, notes=["Cotes non rattachables à ces équipes"]
            )

        fair = remove_vig(mapped)
        if not fair:
            return MarketRead(available=False, notes=["Cotes inexploitables"])

        implied = sum(1 / price for price in mapped.values() if price > 1)
        read = MarketRead(
            available=True,
            probabilities=fair,
            margin=implied - 1.0,
            bookmakers=odds.bookmaker_count,
            dispersion=odds.dispersion,
            drift=dict(odds.movement),
            drift_hours=odds.movement_hours,
        )
        read.favourite = (
            "home" if fair.get("home", 0) >= fair.get("away", 0) else "away"
        )

        if read.bookmakers < 3:
            read.notes.append("Peu de bookmakers pour ce match")
        if read.dispersion is not None and read.dispersion > 0.10:
            read.notes.append("Les bookmakers ne s'accordent pas")
        if read.margin is not None and read.margin > 0.12:
            read.notes.append("Marge inhabituellement élevée")

        strongest = read.strongest_drift()
        if strongest and abs(strongest[1]) >= cfg.AGENT.odds_drift_threshold:
            direction = "s'allongent" if strongest[1] > 0 else "se raccourcissent"
            read.notes.append(f"Les cotes de {strongest[0]} {direction}")
        return read

    # ------------------------------------------------------------------
    @staticmethod
    def divergence(read: MarketRead, model_probs: dict[str, float]) -> float:
        """Écart maximal entre notre estimation et celle du marché."""
        if not read.available or not model_probs:
            return 0.0
        return max(
            abs(model_probs.get(key, 0.0) - value)
            for key, value in read.probabilities.items()
        )
