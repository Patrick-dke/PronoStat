"""Module d'auto-évaluation.

Responsabilité unique : après coup, l'agent note **sa propre analyse**.

Cinq critères mesurés, tous à partir de faits observables : qualité et
quantité des données, fraîcheur, cohérence entre sources, et stabilité des
probabilités d'une simulation à l'autre. Cette note module ensuite le niveau
de confiance affiché — elle ne modifie jamais les probabilités elles-mêmes.
"""

from __future__ import annotations

from datetime import timezone

import numpy as np

import config as cfg
from agent.contracts import SelfAssessment
from data_sources import Bundle

UTC = timezone.utc


class SelfEvaluator:
    """Évalue la solidité de l'analyse qui vient d'être produite."""

    def assess(
        self,
        bundle: Bundle,
        prediction,
        research_report=None,
        validation=None,
        contradictions=None,
    ) -> SelfAssessment:
        assessment = SelfAssessment()

        assessment.data_quality = self._quality(bundle, research_report)
        assessment.data_quantity = self._quantity(bundle, validation)
        assessment.data_freshness = self._freshness(bundle)
        assessment.source_coherence = self._coherence(contradictions, research_report)
        assessment.probability_stability = self._stability(prediction)

        self._write_notes(assessment, bundle, validation)
        return assessment

    # ------------------------------------------------------------------
    def _quality(self, bundle: Bundle, research_report) -> float:
        """Fiabilité moyenne des sources effectivement retenues."""
        sources = {p.source for p in bundle.provenances}
        if not sources:
            return 0.0
        average = sum(cfg.reliability(s) for s in sources) / len(sources)
        best = max(cfg.reliability(s) for s in sources)
        # La meilleure source compte double : une source de référence tire la
        # qualité vers le haut même accompagnée de sources modestes.
        return round(min(1.0, (average + 2 * best) / 3), 3)

    def _quantity(self, bundle: Bundle, validation) -> float:
        """Volume d'informations réunies, rapporté à ce qu'on espérait."""
        score = 0.0
        if bundle.odds is not None:
            score += 0.30
        n_home = bundle.form_home.n if bundle.form_home else 0
        n_away = bundle.form_away.n if bundle.form_away else 0
        score += 0.30 * min(1.0, min(n_home, n_away) / cfg.FORM_WINDOW)
        if bundle.standings:
            score += 0.20
        if bundle.h2h:
            score += 0.10 + 0.05 * min(1.0, len(bundle.h2h) / 5)
        if bundle.form_home and bundle.form_home.xg_for is not None:
            score += 0.05
        return round(min(1.0, score), 3)

    def _freshness(self, bundle: Bundle) -> float:
        if not bundle.provenances:
            return 0.0
        ages = [p.age_seconds / 3600 for p in bundle.provenances]
        worst = max(ages)
        if worst <= 6:
            return 1.0
        if worst <= 24:
            return 0.85
        if worst <= 48:
            return 0.6
        if worst <= 24 * 7:
            return 0.35
        return 0.15

    def _coherence(self, contradictions, research_report) -> float:
        """1 = tout concorde, 0 = les signaux se contredisent gravement."""
        severity = sum(c.severity for c in (contradictions or []))
        if research_report is not None:
            severity += sum(
                float(getattr(i, "severity", 0.0)) * 0.5
                for i in getattr(research_report, "inconsistencies", []) or []
            )
        return round(max(0.0, 1.0 - severity / 3.0), 3)

    def _stability(self, prediction) -> float:
        """Les probabilités varient-elles d'un lot de simulations à l'autre ?

        Mesure réelle : les tirages sont découpés en lots indépendants et on
        observe la dispersion de la probabilité de victoire à domicile. Une
        dispersion large signale qu'il faudrait plus de simulations — ou que
        le match est intrinsèquement indécis.
        """
        trace = getattr(prediction, "samples", None)
        if trace is None:
            return 0.6  # non mesurable : on reste neutre plutôt que d'inventer
        home, away = trace.get("home"), trace.get("away")
        if home is None or away is None or home.size < 1000:
            return 0.6
        batches = cfg.AGENT.stability_batches
        chunks = np.array_split(home > away, batches)
        rates = np.array([float(np.mean(c)) for c in chunks if c.size])
        if rates.size < 2:
            return 0.6
        spread = float(np.max(rates) - np.min(rates))
        # 2 points d'écart entre lots = excellent ; 10 points = médiocre.
        return round(float(max(0.0, min(1.0, 1.0 - (spread - 0.02) / 0.08))), 3)

    # ------------------------------------------------------------------
    def _write_notes(self, assessment: SelfAssessment, bundle: Bundle, validation) -> None:
        """Notes internes, reprises telles quelles dans l'interface si utiles."""
        if assessment.data_quantity < 0.4:
            assessment.notes.append("Peu d'informations disponibles sur ce match")
        if assessment.data_freshness < 0.5:
            assessment.notes.append("Données pas toutes récentes")
        if assessment.source_coherence < 0.6:
            assessment.notes.append("Signaux partiellement contradictoires")
        if assessment.probability_stability < 0.5:
            assessment.notes.append("Issue très ouverte d'une simulation à l'autre")
        if bundle.odds is None:
            assessment.notes.append("Aucune cote pour ancrer l'estimation")
        for warning in (getattr(validation, "warnings", []) or [])[:2]:
            assessment.notes.append(warning)
