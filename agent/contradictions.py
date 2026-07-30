"""Module de détection des contradictions.

Responsabilité unique : chercher **activement** les signaux qui se
contredisent. Un désaccord entre indicateurs fiables est une information en
soi : il ne doit pas être moyenné en silence, mais faire baisser la confiance.

Chaque contradiction est formulée en une ligne compréhensible et porte une
gravité, qui alimente ensuite la pénalité de confiance.
"""

from __future__ import annotations

import config as cfg
from agent.contracts import Contradiction, FactorReport
from agent.market import MarketRead
from data_sources import Bundle


class ContradictionDetector:
    """Confronte les signaux entre eux et signale les désaccords."""

    def __init__(self, pattern_detector=None):
        # Point d'insertion prévu : un modèle capable de repérer des motifs
        # que ces règles explicites ne couvrent pas.
        self.pattern_detector = pattern_detector

    # ------------------------------------------------------------------
    def detect(
        self,
        bundle: Bundle,
        prediction,
        factors: FactorReport,
        market: MarketRead,
        research_report=None,
    ) -> list[Contradiction]:
        found: list[Contradiction] = []

        found += self._stats_versus_market(prediction, market)
        found += self._drift_against_favourite(bundle, market, prediction)
        found += self._form_versus_injuries(bundle, factors)
        found += self._history_versus_present(factors)
        found += self._internal_disagreement(factors)
        found += self._market_internal(market)
        found += self._from_research(research_report)

        if self.pattern_detector is not None:
            try:
                found += list(self.pattern_detector.detect(bundle, prediction) or [])
            except Exception:
                pass  # un détecteur défaillant ne doit jamais bloquer l'analyse

        # Une même cause peut être repérée deux fois : on garde la plus grave.
        unique: dict[str, Contradiction] = {}
        for item in found:
            previous = unique.get(item.key)
            if previous is None or item.severity > previous.severity:
                unique[item.key] = item
        return sorted(unique.values(), key=lambda c: -c.severity)

    # ------------------------------------------------------------------
    def _stats_versus_market(self, prediction, market: MarketRead) -> list[Contradiction]:
        """Nos statistiques désignent une équipe, le marché en désigne une autre."""
        if not market.available or prediction is None:
            return []
        model = prediction.outcome_probs
        model_favourite = "home" if model.get("home", 0) >= model.get("away", 0) else "away"
        divergence = MarketAnalystDivergence(market, model)
        if model_favourite != market.favourite and divergence >= 0.08:
            return [
                Contradiction(
                    key="favori_oppose",
                    text="Nos statistiques et les bookmakers ne désignent pas le même favori",
                    severity=min(1.0, divergence / 0.25),
                    sources=("statistiques", "cotes"),
                )
            ]
        if divergence >= cfg.AGENT.market_divergence_threshold:
            return [
                Contradiction(
                    key="ecart_marche",
                    text="Notre estimation s'écarte nettement de celle des bookmakers",
                    severity=min(1.0, divergence / 0.30),
                    sources=("statistiques", "cotes"),
                )
            ]
        return []

    def _drift_against_favourite(
        self, bundle: Bundle, market: MarketRead, prediction
    ) -> list[Contradiction]:
        """Une équipe favorite dont la cote s'allonge : le marché doute."""
        strongest = market.strongest_drift()
        if not strongest or bundle.odds is None:
            return []
        label, delta = strongest
        if abs(delta) < cfg.AGENT.odds_drift_threshold:
            return []
        # La cote monte (delta > 0) alors que l'équipe est notre favorite.
        favourite_name = getattr(prediction, "favorite", None)
        if delta > 0 and favourite_name and label.strip() == favourite_name.strip():
            return [
                Contradiction(
                    key="cote_favori_monte",
                    text=f"{label} est notre favori, mais sa cote s'allonge",
                    severity=min(1.0, abs(delta) / 0.15),
                    sources=("cotes", "statistiques"),
                )
            ]
        return []

    def _form_versus_injuries(
        self, bundle: Bundle, factors: FactorReport
    ) -> list[Contradiction]:
        """Excellente forme d'un côté, absences signalées de l'autre."""
        if not bundle.news:
            return []
        forme = next((f for f in factors.factors if f.key == "forme_recente"), None)
        if forme is None or not forme.available or abs(forme.value) < 0.25:
            return []
        favoured = bundle.home if forme.value > 0 else bundle.away
        concerned = [n for n in bundle.news if n.team.strip() == favoured.strip()]
        if not concerned:
            return []
        return [
            Contradiction(
                key="forme_vs_absences",
                text=f"{favoured} est en forme mais une absence est signalée",
                severity=0.45,
                sources=("forme", "actualité"),
            )
        ]

    def _history_versus_present(self, factors: FactorReport) -> list[Contradiction]:
        """L'historique dit une chose, la forme actuelle dit l'inverse."""
        h2h = next((f for f in factors.factors if f.key == "confrontations"), None)
        forme = next((f for f in factors.factors if f.key == "forme_recente"), None)
        if not h2h or not forme or not h2h.available or not forme.available:
            return []
        if h2h.value * forme.value >= 0:
            return []
        gap = abs(h2h.value) + abs(forme.value)
        if gap < 0.6:
            return []
        return [
            Contradiction(
                key="historique_vs_forme",
                text="L'historique des confrontations contredit la forme du moment",
                severity=min(1.0, gap / 1.6),
                sources=("confrontations", "forme"),
            )
        ]

    def _internal_disagreement(self, factors: FactorReport) -> list[Contradiction]:
        """Les critères solides se répartissent des deux côtés."""
        strong = [
            f for f in factors.available_factors()
            if f.strength >= 0.20 and abs(f.value) >= 0.15
        ]
        if len(strong) < 3:
            return []
        for_home = sum(1 for f in strong if f.value > 0)
        for_away = len(strong) - for_home
        if min(for_home, for_away) == 0:
            return []
        balance = min(for_home, for_away) / len(strong)
        if balance < 0.4:
            return []
        return [
            Contradiction(
                key="criteres_partages",
                text="Les indicateurs se partagent entre les deux équipes",
                severity=0.35 + 0.3 * balance,
                sources=("statistiques",),
            )
        ]

    def _market_internal(self, market: MarketRead) -> list[Contradiction]:
        if not market.available or market.dispersion is None:
            return []
        if market.dispersion <= 0.10:
            return []
        return [
            Contradiction(
                key="bookmakers_desaccord",
                text="Les bookmakers proposent des cotes très différentes",
                severity=min(1.0, market.dispersion * 4),
                sources=("cotes",),
            )
        ]

    def _from_research(self, research_report) -> list[Contradiction]:
        """Reprend les incohérences relevées pendant la collecte."""
        if research_report is None:
            return []
        out = []
        for issue in getattr(research_report, "inconsistencies", []) or []:
            out.append(
                Contradiction(
                    key=f"collecte_{issue.field}",
                    text=issue.public_text(),
                    severity=float(getattr(issue, "severity", 0.3)),
                    sources=tuple(getattr(issue, "sources", ())),
                )
            )
        return out


def MarketAnalystDivergence(market: MarketRead, model_probs: dict[str, float]) -> float:
    """Écart maximal entre notre estimation et le marché (fonction utilitaire)."""
    if not market.available or not model_probs:
        return 0.0
    return max(
        abs(model_probs.get(key, 0.0) - value)
        for key, value in market.probabilities.items()
    )
