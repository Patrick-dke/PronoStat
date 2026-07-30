"""Module de décision finale.

Responsabilité unique : transformer tout ce qui précède en **une** recommandation
assortie de sa probabilité, de sa confiance, de ses facteurs déterminants et de
ses risques.

La décision n'est pas le résultat d'un seul calcul : elle part du marché
recommandé par la simulation, l'ajuste avec les facteurs que la simulation
n'a pas consommés, puis abaisse la confiance en fonction des contradictions
relevées et de l'auto-évaluation. L'agent peut aussi **s'abstenir** quand
aucune recommandation n'est défendable.
"""

from __future__ import annotations

import config as cfg
from agent.contracts import (
    Contradiction,
    Decision,
    Factor,
    FactorReport,
    Scenario,
    SelfAssessment,
)
from engine import MarketLine, Prediction, _pick_family


class TemplateNarrator:
    """Rédige une justification courte à partir des chiffres calculés.

    Aucune tournure n'introduit de fait absent des données : chaque phrase
    reprend une valeur mesurée. C'est le point d'insertion prévu pour un
    modèle de langage, qui devra respecter la même règle.
    """

    def write(self, decision: Decision, prediction: Prediction) -> str:
        parts: list[str] = []
        drivers = [f for f in decision.key_factors if abs(f.value) >= 0.10][:2]
        if drivers:
            parts.append(
                "Décision portée par " + " et ".join(f.label.lower() for f in drivers) + "."
            )
        else:
            parts.append("Aucun critère ne se détache nettement.")

        if decision.is_value and decision.odds:
            parts.append(
                f"Cote {decision.odds:.2f} jugée généreuse au vu de notre estimation."
            )
        elif prediction.market_probs:
            parts.append("Estimation conforme au consensus des bookmakers.")
        elif getattr(prediction, "used_market_reference", False):
            parts.append(
                "Aucune cote publiée : estimation ancrée sur ce que le marché "
                "accordait à ces équipes cette saison."
            )
        else:
            parts.append("Aucune cote disponible pour recouper l'estimation.")

        if decision.contradictions:
            parts.append(f"Réserve : {decision.contradictions[0].text.lower()}.")
        return " ".join(parts[:3])


class DecisionMaker:
    """Assemble la décision finale de l'agent."""

    def __init__(self, narrator=None):
        self.narrator = narrator or TemplateNarrator()

    # ------------------------------------------------------------------
    def decide(
        self,
        prediction: Prediction,
        factors: FactorReport,
        contradictions: list[Contradiction],
        scenarios: list[Scenario],
        assessment: SelfAssessment,
        fingerprint: str = "",
    ) -> Decision:
        line = self._select(prediction, factors)
        if line is None:
            return self._abstain(factors, contradictions, scenarios, assessment, fingerprint)

        probability = self._adjust(line, factors, prediction)
        confidence = self._confidence(prediction, contradictions, assessment)

        family = _pick_family(line.key)
        decision = Decision(
            recommendation=line.label,
            market=family[0] if family else "Marché",
            probability=probability,
            confidence=confidence,
            key_factors=factors.top(cfg.AGENT.top_factors),
            risks=scenarios[: cfg.AGENT.top_risks],
            contradictions=contradictions[:3],
            assessment=assessment,
            odds=line.odds,
            edge=line.edge,
            is_value=line.is_value,
            fingerprint=fingerprint,
        )
        decision.rationale = self.narrator.write(decision, prediction)
        return decision

    # ------------------------------------------------------------------
    def _select(self, prediction: Prediction, factors: FactorReport) -> MarketLine | None:
        """Repart du marché retenu par la simulation, jamais d'un autre calcul."""
        pick = prediction.main_pick
        if pick is None:
            return None
        return next((l for l in prediction.lines if l.key == pick.key), None)

    def _adjust(
        self, line: MarketLine, factors: FactorReport, prediction: Prediction
    ) -> float:
        """Applique les facteurs que la simulation n'a PAS consommés.

        L'ajustement est borné par `AGENT_TILT_STRENGTH` et n'est appliqué que
        si le marché retenu dépend du vainqueur : décaler une probabilité de
        « nombre de buts » selon la dynamique d'une équipe n'aurait pas de sens.
        """
        tilt = factors.tilt
        if abs(tilt) < 1e-6:
            return line.prob

        directional = line.key.startswith(("1x2_", "ml_", "dc_", "spread_", "hcp_", "puckline_"))
        if not directional:
            return line.prob

        favours_home = "home" in line.key or line.label == prediction.home
        signed = tilt if favours_home else -tilt
        shift = signed * cfg.AGENT.tilt_strength * factors.coverage
        return float(min(0.98, max(0.02, line.prob + shift)))

    def _confidence(
        self,
        prediction: Prediction,
        contradictions: list[Contradiction],
        assessment: SelfAssessment,
    ) -> float:
        """Confiance /10 : la note du moteur, corrigée par l'agent.

        Deux corrections : les contradictions font baisser la note, et
        l'auto-évaluation la module dans une fourchette resserrée.
        """
        base = prediction.confidence.score
        penalty = min(
            cfg.AGENT.contradiction_penalty_max,
            sum(c.severity for c in contradictions) * 0.9,
        )
        # 0,5 d'auto-évaluation laisse la note inchangée.
        modifier = 0.85 + 0.30 * assessment.score
        return round(max(0.0, min(10.0, (base - penalty) * modifier)), 1)

    def _abstain(
        self, factors, contradictions, scenarios, assessment, fingerprint
    ) -> Decision:
        decision = Decision(
            recommendation="Aucun pronostic défendable",
            market="—",
            probability=0.0,
            confidence=0.0,
            key_factors=factors.top(cfg.AGENT.top_factors),
            risks=scenarios[: cfg.AGENT.top_risks],
            contradictions=contradictions[:3],
            assessment=assessment,
            abstained=True,
            fingerprint=fingerprint,
        )
        decision.rationale = (
            "Les données disponibles ne permettent pas de dégager une "
            "recommandation exploitable sur ce match."
        )
        return decision
