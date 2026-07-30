"""Orchestration de l'agent d'analyse.

Enchaîne les modules spécialisés dans un ordre fixe, chacun ne faisant qu'une
chose :

    collecte → validation → fusion → analyse statistique → analyse du marché
      → simulation → détection des contradictions → scénarios alternatifs
      → auto-évaluation → décision finale

Chaque étape est isolée : si l'une échoue, l'agent continue avec ce qu'il a et
abaisse sa confiance. Il ne s'arrête jamais sur une source manquante et
n'invente jamais de donnée pour combler un trou.

Le résultat est **reproductible** : à données identiques, la graine aléatoire
dérive de l'empreinte des données, donc l'analyse est identique.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import config as cfg
from agent.contracts import Decision, FactorReport, SelfAssessment
from agent.contradictions import ContradictionDetector
from agent.decision import DecisionMaker
from agent.factors import FactorEngine
from agent.introspection import SelfEvaluator
from agent.market import MarketAnalyst, MarketRead
from agent.memory import PredictionLedger
from agent.scenarios import ScenarioExplorer
from agent.validation import DataValidator, ValidationReport
from config import Competition
from data_sources import Bundle
from engine import Prediction, analyse

UTC = timezone.utc
log = logging.getLogger("pronostat.agent")


@dataclass
class AnalysisResult:
    """Tout ce que l'agent produit pour une rencontre."""

    decision: Decision
    prediction: Prediction
    bundle: Bundle
    factors: FactorReport
    market: MarketRead
    validation: ValidationReport
    research: Any = None
    duration_s: float = 0.0
    steps: list[str] = field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        """Sortie JSON — couture prévue pour une future API HTTP."""
        return {
            "match": {
                "sport": self.prediction.sport,
                "competition": getattr(self.bundle.competition, "label", None),
                "home": self.prediction.home,
                "away": self.prediction.away,
            },
            "decision": self.decision.as_payload(),
            "probabilities": {
                k: round(v, 4) for k, v in self.prediction.outcome_probs.items()
            },
            "market": {
                "available": self.market.available,
                "bookmakers": self.market.bookmakers,
                "probabilities": {
                    k: round(v, 4) for k, v in self.market.probabilities.items()
                },
            },
            "data": {
                "usable": self.validation.usable,
                "discarded": [
                    {"field": name, "reason": reason}
                    for name, reason in self.validation.discarded
                ],
            },
            "duration_s": round(self.duration_s, 2),
        }


class AnalysisAgent:
    """Agent d'analyse sportive autonome.

    Les composants sont injectables : remplacer `score_model`,
    `weighting_policy`, `pattern_detector` ou `narrator` par une
    implémentation fondée sur un modèle d'IA ne demande aucune modification
    du reste de l'application.
    """

    def __init__(
        self,
        hub,
        *,
        score_model=None,
        weighting_policy=None,
        pattern_detector=None,
        narrator=None,
        ledger: PredictionLedger | None = None,
    ):
        self.hub = hub
        self.score_model = score_model          # None = moteur statistique interne
        self.validator = DataValidator()
        self.factor_engine = FactorEngine(weighting_policy)
        self.market_analyst = MarketAnalyst()
        self.contradiction_detector = ContradictionDetector(pattern_detector)
        self.scenario_explorer = ScenarioExplorer()
        self.self_evaluator = SelfEvaluator()
        self.decision_maker = DecisionMaker(narrator)
        self.ledger = ledger or PredictionLedger()

    # ------------------------------------------------------------------
    def analyse_match(
        self,
        comp: Competition,
        home: str,
        away: str,
        *,
        record: bool = True,
    ) -> AnalysisResult:
        started = datetime.now(UTC)
        steps: list[str] = []

        # --- 1. collecte + fusion (module de recherche approfondie) ---
        bundle, research = self._collect(comp, home, away, steps)

        # --- 2. validation : écarter ce qui n'est pas exploitable ---
        validation = self._step(
            steps, "validation", lambda: self.validator.validate(bundle),
            default=ValidationReport(),
        )

        # --- 3. simulation statistique (remplaçable par un modèle appris) ---
        prediction = self._simulate(bundle, research, steps)

        # --- 4. lecture du marché ---
        market = self._step(
            steps, "marché", lambda: self.market_analyst.read(bundle),
            default=MarketRead(),
        )

        # --- 5. raisonnement multicritère ---
        factors = self._step(
            steps, "facteurs",
            lambda: self.factor_engine.evaluate(bundle, prediction, research),
            default=FactorReport(),
        )

        # --- 6. contradictions ---
        contradictions = self._step(
            steps, "contradictions",
            lambda: self.contradiction_detector.detect(
                bundle, prediction, factors, market, research
            ),
            default=[],
        )

        # --- 7. scénarios alternatifs ---
        pick_key = getattr(prediction.main_pick, "key", None)
        scenarios = self._step(
            steps, "scénarios",
            lambda: self.scenario_explorer.explore(prediction, pick_key),
            default=[],
        )

        # --- 8. auto-évaluation ---
        assessment = self._step(
            steps, "auto-évaluation",
            lambda: self.self_evaluator.assess(
                bundle, prediction, research, validation, contradictions
            ),
            default=SelfAssessment(),
        )

        # --- 9. décision finale ---
        decision = self.decision_maker.decide(
            prediction, factors, contradictions, scenarios, assessment,
            fingerprint=self._fingerprint(bundle),
        )
        steps.append("décision")

        # La confiance de l'agent remplace celle du moteur dans l'affichage.
        prediction.confidence.score = decision.confidence
        if prediction.main_pick is not None:
            prediction.main_pick.confidence = decision.confidence
            prediction.main_pick.probability = decision.probability
            prediction.main_pick.label = decision.recommendation

        result = AnalysisResult(
            decision=decision,
            prediction=prediction,
            bundle=bundle,
            factors=factors,
            market=market,
            validation=validation,
            research=research,
            duration_s=(datetime.now(UTC) - started).total_seconds(),
            steps=steps,
        )

        if record and not decision.abstained:
            try:
                self.ledger.record(decision, prediction, getattr(comp, "label", ""))
            except Exception as exc:  # l'archivage ne doit jamais bloquer
                log.debug("journalisation impossible : %s", exc)

        return result

    # ------------------------------------------------------------------
    # Étapes
    # ------------------------------------------------------------------
    def _collect(self, comp, home, away, steps) -> tuple[Bundle, Any]:
        try:
            bundle, research = self.hub.investigate(comp, home, away)
            steps.append("collecte")
            return bundle, research
        except Exception as exc:
            log.warning("collecte en échec : %s", exc)
            steps.append("collecte (partielle)")
            return Bundle(sport=comp.sport, home=home, away=away, competition=comp), None

    def _simulate(self, bundle: Bundle, research, steps) -> Prediction:
        seed = self._seed(bundle)
        if self.score_model is not None:
            try:
                prediction = self.score_model.predict(bundle, seed=seed)
                steps.append("simulation (modèle externe)")
                return prediction
            except Exception as exc:
                log.warning("modèle externe en échec, repli statistique : %s", exc)
        prediction = analyse(bundle, seed=seed, report=research)
        steps.append("simulation")
        return prediction

    def _step(self, steps: list[str], name: str, fn, default):
        """Exécute une étape sans jamais laisser une panne interrompre l'analyse."""
        try:
            value = fn()
            steps.append(name)
            return value
        except Exception as exc:
            log.warning("étape « %s » en échec : %s", name, exc)
            steps.append(f"{name} (ignorée)")
            return default

    # ------------------------------------------------------------------
    # Reproductibilité
    # ------------------------------------------------------------------
    def _fingerprint(self, bundle: Bundle) -> str:
        """Empreinte des données d'entrée : deux analyses identiques la partagent."""
        parts: list[str] = [
            bundle.sport,
            getattr(bundle.competition, "key", ""),
            bundle.home,
            bundle.away,
        ]
        for form in (bundle.form_home, bundle.form_away):
            if form is None:
                parts.append("∅")
                continue
            parts.append(
                "/".join(
                    f"{m.date:%Y%m%d}:{m.scored:g}-{m.conceded:g}" for m in form.matches
                )
            )
        if bundle.odds is not None:
            parts.append(
                ",".join(f"{k}={v:.3f}" for k, v in sorted(bundle.odds.h2h.items()))
            )
        if bundle.standings:
            parts.append(f"table:{len(bundle.standings)}")
        raw = "|".join(parts).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]

    def _seed(self, bundle: Bundle) -> int | None:
        """Graine dérivée des données : même match, mêmes données, même résultat."""
        if not cfg.AGENT.deterministic:
            return None
        return int(self._fingerprint(bundle), 16) % (2**31 - 1)
