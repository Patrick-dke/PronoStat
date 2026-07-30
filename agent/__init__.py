"""Agent d'analyse sportive autonome.

Point d'entrée unique :

    from agent import AnalysisAgent
    result = AnalysisAgent(hub).analyse_match(competition, "Arsenal", "Chelsea")
    print(result.decision.recommendation, result.decision.probability)

L'agent n'est pas un assistant conversationnel : il ne dialogue pas, il
décide. Il enchaîne des modules spécialisés (collecte, validation, fusion,
statistiques, marché, simulation, contradictions, scénarios, auto-évaluation,
décision) et produit une recommandation unique, assortie de sa probabilité,
de son niveau de confiance, de ses facteurs déterminants et de ses risques.

Trois garanties tenues par construction :
  * il fonctionne même si des sources manquent — il baisse alors sa confiance ;
  * il n'invente jamais une donnée absente ;
  * il ne présente jamais une prédiction comme une certitude.
"""

from agent.contracts import (
    Contradiction,
    Decision,
    Factor,
    FactorReport,
    NarrativeWriter,
    PatternDetector,
    Scenario,
    ScoreModel,
    SelfAssessment,
    WeightingPolicy,
)
from agent.memory import (
    LedgerEntry,
    PerformanceAnalyst,
    PerformanceReport,
    PredictionLedger,
    TuningAdvisor,
    TuningProposal,
    evaluate_market,
)
from agent.pipeline import AnalysisAgent, AnalysisResult

__all__ = [
    "AnalysisAgent",
    "AnalysisResult",
    "Decision",
    "Factor",
    "FactorReport",
    "Contradiction",
    "Scenario",
    "SelfAssessment",
    # interfaces remplaçables par un modèle d'IA
    "ScoreModel",
    "WeightingPolicy",
    "PatternDetector",
    "NarrativeWriter",
    # mémoire et apprentissage
    "PredictionLedger",
    "LedgerEntry",
    "PerformanceAnalyst",
    "PerformanceReport",
    "TuningAdvisor",
    "TuningProposal",
    "evaluate_market",
]
