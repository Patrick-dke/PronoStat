"""Contrats de l'agent d'analyse.

Ce module ne contient aucune logique : uniquement les **types échangés entre
modules** et les **interfaces remplaçables**.

C'est ici que se trouvent les points d'insertion prévus pour un modèle
d'intelligence artificielle. Chaque interface a aujourd'hui une implémentation
déterministe ; la remplacer suffit à changer le comportement de l'agent, sans
toucher au reste de l'application :

    agent = AnalysisAgent(score_model=MonModeleIA())

Les quatre points d'extension prévus :
    * `ScoreModel`      — prédiction de la distribution des scores
    * `WeightingPolicy` — pondération des facteurs
    * `PatternDetector` — détection de motifs complexes
    * `NarrativeWriter` — synthèse en langage naturel
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import config as cfg


# ==========================================================================
# Types échangés entre les modules
# ==========================================================================
@dataclass
class Factor:
    """Un critère du raisonnement, évalué pour un match donné.

    `value` est signé et borné à [-1, 1] : positif = penche pour l'équipe à
    domicile, négatif = pour l'extérieur, 0 = neutre. `confidence` dit à quel
    point la donnée sous-jacente est solide (échantillon, fraîcheur).
    """

    key: str
    label: str
    value: float
    weight: float
    confidence: float
    detail: str = ""
    in_model: bool = False      # déjà consommé par la simulation ?
    available: bool = True

    @property
    def contribution(self) -> float:
        """Poids effectif de ce facteur dans la décision."""
        return self.value * self.weight * self.confidence

    @property
    def direction(self) -> str:
        if abs(self.value) < 0.05:
            return "neutre"
        return "domicile" if self.value > 0 else "extérieur"

    @property
    def strength(self) -> float:
        """Intensité absolue, pour classer les facteurs déterminants."""
        return abs(self.value) * self.weight * self.confidence


@dataclass
class FactorReport:
    """Résultat du raisonnement multicritère."""

    factors: list[Factor] = field(default_factory=list)
    context: list[Factor] = field(default_factory=list)

    def available_factors(self) -> list[Factor]:
        return [f for f in self.factors if f.available]

    @property
    def tilt(self) -> float:
        """Inclinaison globale issue des seuls facteurs hors simulation.

        Les facteurs déjà consommés par la simulation sont exclus : les
        compter une seconde fois fausserait la probabilité.
        """
        usable = [f for f in self.available_factors() if not f.in_model]
        total_weight = sum(f.weight * f.confidence for f in usable)
        if total_weight <= 0:
            return 0.0
        return sum(f.contribution for f in usable) / total_weight

    @property
    def coverage(self) -> float:
        """Part du poids total effectivement documentée par des données."""
        total = sum(f.weight for f in self.factors)
        got = sum(f.weight for f in self.available_factors())
        return got / total if total else 0.0

    def top(self, n: int = 4, in_model: bool | None = None) -> list[Factor]:
        pool = [f for f in self.available_factors() if abs(f.value) >= 0.05]
        if in_model is not None:
            pool = [f for f in pool if f.in_model is in_model]
        return sorted(pool, key=lambda f: -f.strength)[:n]


@dataclass
class Contradiction:
    """Deux signaux fiables qui pointent dans des directions opposées."""

    key: str
    text: str                   # formulation grand public, une ligne
    severity: float             # 0 = anecdotique, 1 = majeur
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.severity = max(0.0, min(1.0, self.severity))


@dataclass
class Scenario:
    """Un déroulement alternatif susceptible de faire tomber le pronostic."""

    key: str
    text: str
    probability: float
    breaks_pick: bool = True    # ce scénario invalide-t-il la décision ?

    @property
    def impact(self) -> float:
        return self.probability if self.breaks_pick else self.probability * 0.3


@dataclass
class SelfAssessment:
    """Auto-évaluation de l'agent sur sa propre analyse."""

    data_quality: float = 0.0       # fiabilité moyenne des sources retenues
    data_quantity: float = 0.0      # volume d'informations réunies
    data_freshness: float = 0.0     # fraîcheur
    source_coherence: float = 0.0   # accord entre sources
    probability_stability: float = 0.0  # stabilité des simulations
    notes: list[str] = field(default_factory=list)

    # Poids des cinq critères. Volontairement une liste de paires : avec un
    # dictionnaire indexé par la valeur, deux critères égaux fusionneraient
    # et la pondération serait faussée.
    WEIGHTS: tuple[tuple[str, float], ...] = (
        ("data_quality", 0.30),
        ("data_quantity", 0.20),
        ("data_freshness", 0.15),
        ("source_coherence", 0.20),
        ("probability_stability", 0.15),
    )

    @property
    def score(self) -> float:
        """Note globale entre 0 et 1, moyenne pondérée des cinq critères."""
        return round(
            sum(getattr(self, name) * weight for name, weight in self.WEIGHTS), 3
        )

    @property
    def label(self) -> str:
        score = self.score
        if score >= 0.75:
            return "Analyse solide"
        if score >= 0.55:
            return "Analyse correcte"
        if score >= 0.35:
            return "Analyse partielle"
        return "Analyse limitée"

    def as_dict(self) -> dict[str, float]:
        return {
            "qualité des données": round(self.data_quality, 3),
            "quantité de données": round(self.data_quantity, 3),
            "fraîcheur": round(self.data_freshness, 3),
            "cohérence des sources": round(self.source_coherence, 3),
            "stabilité des probabilités": round(self.probability_stability, 3),
        }


@dataclass
class Decision:
    """Décision finale de l'agent : le produit de tout le processus."""

    recommendation: str             # ce qui est recommandé, en clair
    market: str                     # famille de marché
    probability: float
    confidence: float               # sur 10
    key_factors: list[Factor] = field(default_factory=list)
    risks: list[Scenario] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    assessment: SelfAssessment = field(default_factory=SelfAssessment)
    rationale: str = ""             # justification très concise
    odds: float | None = None
    edge: float | None = None
    is_value: bool = False
    abstained: bool = False         # aucune recommandation défendable
    fingerprint: str = ""           # empreinte des données (reproductibilité)

    @property
    def strength(self) -> str:
        if self.abstained:
            return "Sans opinion"
        if self.probability >= 0.70:
            return "Fort"
        if self.probability >= 0.55:
            return "Solide"
        if self.probability >= 0.45:
            return "Prudent"
        return "Incertain"

    def as_payload(self) -> dict[str, Any]:
        """Sortie sérialisable — la couture prévue pour une future API HTTP."""
        return {
            "recommendation": self.recommendation,
            "market": self.market,
            "probability": round(self.probability, 4),
            "confidence": round(self.confidence, 2),
            "strength": self.strength,
            "abstained": self.abstained,
            "odds": self.odds,
            "edge": round(self.edge, 4) if self.edge is not None else None,
            "is_value": self.is_value,
            "rationale": self.rationale,
            "key_factors": [
                {
                    "label": f.label,
                    "direction": f.direction,
                    "value": round(f.value, 3),
                    "weight": f.weight,
                    "detail": f.detail,
                }
                for f in self.key_factors
            ],
            "risks": [
                {"text": s.text, "probability": round(s.probability, 4)}
                for s in self.risks
            ],
            "contradictions": [
                {"text": c.text, "severity": round(c.severity, 2)}
                for c in self.contradictions
            ],
            "self_assessment": self.assessment.as_dict(),
            "fingerprint": self.fingerprint,
        }


# ==========================================================================
# Interfaces remplaçables (points d'insertion pour un modèle d'IA)
# ==========================================================================
@runtime_checkable
class ScoreModel(Protocol):
    """Produit la distribution des scores d'un match.

    L'implémentation par défaut est le modèle Poisson/Dixon-Coles calibré sur
    le marché. Un modèle appris pourrait le remplacer intégralement : il doit
    seulement renvoyer un objet `Prediction` d'`engine.py`.
    """

    def predict(self, bundle: Any, seed: int | None = None) -> Any:
        ...


@runtime_checkable
class WeightingPolicy(Protocol):
    """Décide du poids de chaque facteur.

    Par défaut, les poids fixes de `config.FACTOR_WEIGHTS`. Un modèle appris
    pourrait les moduler selon le contexte (compétition, saison, effectif de
    données disponibles).
    """

    def weights(self, context: dict[str, Any]) -> dict[str, float]:
        ...


@runtime_checkable
class PatternDetector(Protocol):
    """Repère des configurations complexes que les règles simples manquent.

    L'implémentation par défaut est vide : aucune donnée ne permet aujourd'hui
    d'affirmer l'existence d'un motif sans l'inventer.
    """

    def detect(self, bundle: Any, prediction: Any) -> list[Contradiction]:
        ...


@runtime_checkable
class NarrativeWriter(Protocol):
    """Rédige la justification concise de la décision.

    Par défaut, un gabarit déterministe alimenté par les chiffres calculés.
    Un modèle de langage pourrait la reformuler — sans jamais introduire de
    fait absent des données.
    """

    def write(self, decision: Decision, prediction: Any) -> str:
        ...


# ==========================================================================
# Politique de pondération par défaut
# ==========================================================================
class ConfiguredWeights:
    """Poids lus dans la configuration, identiques pour tous les matchs."""

    def weights(self, context: dict[str, Any] | None = None) -> dict[str, float]:
        return {spec.key: spec.weight for spec in cfg.FACTOR_WEIGHTS}

    @staticmethod
    def specs() -> dict[str, cfg.FactorSpec]:
        return {spec.key: spec for spec in cfg.FACTOR_WEIGHTS}
