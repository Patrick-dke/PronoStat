"""Tests de l'agent de décision : facteurs, contradictions, scénarios,
auto-évaluation, décision, robustesse et reproductibilité."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
import data_sources as ds  # noqa: E402
import engine  # noqa: E402
from agent import AnalysisAgent  # noqa: E402
from agent.contracts import (  # noqa: E402
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
from agent.contradictions import ContradictionDetector  # noqa: E402
from agent.decision import DecisionMaker  # noqa: E402
from agent.factors import FactorEngine  # noqa: E402
from agent.introspection import SelfEvaluator  # noqa: E402
from agent.market import MarketAnalyst  # noqa: E402
from agent.scenarios import ScenarioExplorer  # noqa: E402
from agent.validation import DataValidator  # noqa: E402

UTC = timezone.utc


# ==========================================================================
# Fixtures
# ==========================================================================
def comp(sport="football", key="premier_league"):
    found = cfg.competition(sport, key)
    assert found is not None
    return found


def make_form(team, scored, conceded, sport="football", days_ago_first=3, extras=None):
    now = datetime.now(UTC)
    matches = [
        ds.MatchResult(
            date=now - timedelta(days=days_ago_first + 7 * i),
            opponent=f"Adversaire {i}",
            home=(i % 2 == 0),
            scored=s,
            conceded=c,
            extra=dict((extras or [{}] * len(scored))[i]) if extras else {},
        )
        for i, (s, c) in enumerate(zip(scored, conceded))
    ]
    return ds.TeamForm(team, sport, matches, ds.Provenance("api_football", "T", now))


def make_bundle(sport="football", strong_home=True, odds=None, standings=None, h2h=None):
    bundle = ds.Bundle(sport=sport, home="Équipe A", away="Équipe B",
                       competition=comp(sport, cfg.competitions(sport)[0].key))
    if strong_home:
        bundle.form_home = make_form("Équipe A", [3, 2, 3, 2, 3, 2, 3, 2],
                                     [0, 1, 0, 1, 0, 1, 1, 0], sport)
        bundle.form_away = make_form("Équipe B", [0, 1, 0, 1, 1, 0, 1, 0],
                                     [3, 2, 2, 3, 2, 3, 2, 2], sport)
    else:
        bundle.form_home = make_form("Équipe A", [1, 1, 1, 1, 1, 1, 1, 1],
                                     [1, 1, 1, 1, 1, 1, 1, 1], sport)
        bundle.form_away = make_form("Équipe B", [1, 1, 1, 1, 1, 1, 1, 1],
                                     [1, 1, 1, 1, 1, 1, 1, 1], sport)
    bundle.odds = odds
    bundle.standings = standings or {}
    bundle.h2h = h2h or []
    bundle.league_context = {"avg_per_team": 1.4, "estimated": True, "n": 16}
    bundle.provenances = [ds.Provenance("api_football", "T", datetime.now(UTC))]
    return bundle


def make_odds(h2h, books=8, movement=None, per_book=None):
    return ds.OddsSnapshot(
        home_team="Équipe A", away_team="Équipe B", commence_time=None,
        sport_key="k", provenance=ds.Provenance("the_odds_api", "O", datetime.now(UTC)),
        h2h=h2h, bookmaker_count=books, movement=movement or {},
        movement_hours=12.0 if movement else None, per_book_h2h=per_book or {},
    )


class _Hub:
    """Faux agrégateur : renvoie un dossier préparé, ou lève une panne."""

    def __init__(self, bundle=None, boom=False):
        self._bundle = bundle
        self._boom = boom
        self.providers = []

    def investigate(self, competition, home, away, **kwargs):
        if self._boom:
            raise RuntimeError("toutes les sources sont tombées")
        return self._bundle, None

    def sources_for(self, competition):
        return []

    def league_context(self, competition, bundle):
        return {"avg_per_team": 1.4, "estimated": False, "n": 0}


@pytest.fixture
def ledger(tmp_path):
    from agent.memory import PredictionLedger

    return PredictionLedger(path=tmp_path / "ledger.json")


# ==========================================================================
# Validation
# ==========================================================================
class TestValidation:
    def test_absurd_scores_are_discarded(self):
        bundle = make_bundle()
        bundle.form_home.matches[0].scored = 47      # score impossible au football
        report = DataValidator().validate(bundle)
        assert bundle.form_home.n == 7
        assert any("hors normes" in w for w in report.warnings)

    def test_incoherent_odds_are_rejected(self):
        """Un livre dont les probabilités somment à 60 % est inexploitable."""
        bundle = make_bundle(odds=make_odds({"Équipe A": 5.0, "Équipe B": 5.0}))
        report = DataValidator().validate(bundle)
        assert bundle.odds is None
        assert ("cotes", "cotes incohérentes entre elles") in report.discarded

    def test_sound_odds_are_kept(self):
        bundle = make_bundle(odds=make_odds({"Équipe A": 1.9, "Draw": 3.5, "Équipe B": 4.2}))
        report = DataValidator().validate(bundle)
        assert bundle.odds is not None and report.has("cotes")

    def test_stale_history_is_dropped(self):
        bundle = make_bundle()
        bundle.form_home = make_form("Équipe A", [2, 1], [0, 1], days_ago_first=400)
        report = DataValidator().validate(bundle)
        assert bundle.form_home is None
        assert ("form_home", "historique trop ancien") in report.discarded

    def test_thin_standings_are_dropped(self):
        bundle = make_bundle(standings={
            "Équipe A": ds.Standing("Équipe A", 1, 2, 6, 5, 1),
            "Équipe B": ds.Standing("Équipe B", 2, 2, 3, 3, 3),
        })
        DataValidator().validate(bundle)
        assert bundle.standings == {}

    def test_coverage_reflects_what_survived(self):
        report = DataValidator().validate(make_bundle())
        assert 0.0 < report.coverage < 1.0


# ==========================================================================
# Raisonnement multicritère
# ==========================================================================
class TestFactors:
    def _factors(self, bundle, prediction=None):
        return FactorEngine().evaluate(bundle, prediction)

    def test_every_configured_factor_is_evaluated(self):
        report = self._factors(make_bundle())
        assert {f.key for f in report.factors} == {s.key for s in cfg.FACTOR_WEIGHTS}

    def test_missing_data_yields_unavailable_factor_not_a_guess(self):
        bundle = make_bundle()
        bundle.standings = {}
        report = self._factors(bundle)
        classement = next(f for f in report.factors if f.key == "classement")
        assert not classement.available
        assert classement.value == 0.0
        assert classement.confidence == 0.0
        assert "indisponible" in classement.detail

    def test_strong_home_team_tilts_towards_home(self):
        report = self._factors(make_bundle(strong_home=True))
        forme = next(f for f in report.factors if f.key == "forme_recente")
        assert forme.value > 0.3 and forme.direction == "domicile"

    def test_values_stay_bounded(self):
        report = self._factors(make_bundle(strong_home=True))
        assert all(-1.0 <= f.value <= 1.0 for f in report.factors)
        assert all(0.0 <= f.confidence <= 1.0 for f in report.factors)

    def test_confidence_grows_with_sample_size(self):
        thin = make_bundle()
        thin.form_home = make_form("Équipe A", [3], [0])
        thin.form_away = make_form("Équipe B", [0], [3])
        thick = make_bundle(strong_home=True)
        f_thin = next(f for f in self._factors(thin).factors if f.key == "forme_recente")
        f_thick = next(f for f in self._factors(thick).factors if f.key == "forme_recente")
        assert f_thin.confidence < f_thick.confidence

    def test_tilt_excludes_factors_already_in_the_simulation(self):
        """Recompter la forme récente fausserait la probabilité : elle est exclue."""
        report = self._factors(make_bundle(strong_home=True))
        in_model = [f for f in report.available_factors() if f.in_model]
        assert in_model, "la forme doit bien être évaluée"
        # Le tilt ne doit dépendre que des facteurs hors simulation.
        usable = [f for f in report.available_factors() if not f.in_model]
        expected_weight = sum(f.weight * f.confidence for f in usable)
        if expected_weight:
            expected = sum(f.contribution for f in usable) / expected_weight
            assert math.isclose(report.tilt, expected, abs_tol=1e-9)

    def test_tilt_is_bounded(self):
        report = self._factors(make_bundle(strong_home=True))
        assert -1.0 <= report.tilt <= 1.0

    def test_h2h_factor_reads_the_history(self):
        now = datetime.now(UTC)
        h2h = [ds.MatchResult(now - timedelta(days=90 * i), "Équipe B", True, 3, 0)
               for i in range(5)]
        report = self._factors(make_bundle(h2h=h2h))
        factor = next(f for f in report.factors if f.key == "confrontations")
        assert factor.available and factor.value > 0.3
        assert "5" in factor.detail

    def test_rest_factor_favours_the_rested_team(self):
        bundle = make_bundle()
        bundle.form_home = make_form("Équipe A", [1] * 6, [1] * 6, days_ago_first=7)
        bundle.form_away = make_form("Équipe B", [1] * 6, [1] * 6, days_ago_first=1)
        factor = next(f for f in self._factors(bundle).factors if f.key == "recuperation")
        assert factor.value > 0

    def test_context_factors_never_take_sides(self):
        report = self._factors(make_bundle())
        assert all(f.value == 0.0 for f in report.context)

    def test_top_ranks_by_strength(self):
        report = self._factors(make_bundle(strong_home=True))
        top = report.top(3)
        assert len(top) <= 3
        assert top == sorted(top, key=lambda f: -f.strength)


# ==========================================================================
# Marché
# ==========================================================================
class TestMarket:
    def test_absent_market_is_reported_not_faked(self):
        read = MarketAnalyst().read(make_bundle())
        assert not read.available
        assert read.probabilities == {}

    def test_reads_consensus_and_margin(self):
        bundle = make_bundle(odds=make_odds({"Équipe A": 1.80, "Draw": 3.60, "Équipe B": 4.50}))
        read = MarketAnalyst().read(bundle)
        assert read.available
        assert math.isclose(sum(read.probabilities.values()), 1.0, abs_tol=1e-9)
        assert read.margin > 0
        assert read.favourite == "home"

    def test_agreement_falls_when_bookmakers_diverge(self):
        agree = MarketAnalyst().read(make_bundle(
            odds=make_odds({"Équipe A": 1.8, "Équipe B": 2.1},
                           per_book={"x": {"Équipe A": 1.80}, "y": {"Équipe A": 1.82}})))
        split = MarketAnalyst().read(make_bundle(
            odds=make_odds({"Équipe A": 1.8, "Équipe B": 2.1},
                           per_book={"x": {"Équipe A": 1.50}, "y": {"Équipe A": 2.10}})))
        assert split.agreement < agree.agreement

    def test_drift_is_surfaced(self):
        read = MarketAnalyst().read(make_bundle(
            odds=make_odds({"Équipe A": 1.8, "Équipe B": 2.1},
                           movement={"Équipe A": 0.12})))
        strongest = read.strongest_drift()
        assert strongest and strongest[0] == "Équipe A"
        assert any("allongent" in note for note in read.notes)


# ==========================================================================
# Contradictions
# ==========================================================================
class TestContradictions:
    def _detect(self, bundle, prediction, market=None):
        factors = FactorEngine().evaluate(bundle, prediction)
        market = market or MarketAnalyst().read(bundle)
        return ContradictionDetector().detect(bundle, prediction, factors, market)

    def test_no_contradiction_when_everything_agrees(self):
        bundle = make_bundle(strong_home=True,
                             odds=make_odds({"Équipe A": 1.35, "Draw": 5.0, "Équipe B": 9.0}))
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        found = self._detect(bundle, pred)
        assert not any(c.key in {"favori_oppose", "ecart_marche"} for c in found)

    def test_opposite_favourites_are_flagged(self):
        bundle = make_bundle(strong_home=True,
                             odds=make_odds({"Équipe A": 6.0, "Draw": 4.2, "Équipe B": 1.50}))
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        # Le modèle est ancré au marché : on force le désaccord pour le test.
        # `favorite` se déduit des probabilités, il n'y a rien d'autre à poser.
        pred.outcome_probs = {"home": 0.62, "draw": 0.20, "away": 0.18}
        assert pred.favorite == "Équipe A"
        found = self._detect(bundle, pred)
        assert any(c.key in {"favori_oppose", "ecart_marche"} for c in found)

    def test_history_against_form_is_flagged(self):
        now = datetime.now(UTC)
        # L'historique écrase l'équipe à domicile, sa forme actuelle l'inverse.
        h2h = [ds.MatchResult(now - timedelta(days=120 * i), "Équipe B", True, 0, 3)
               for i in range(5)]
        bundle = make_bundle(strong_home=True, h2h=h2h)
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        found = self._detect(bundle, pred)
        assert any(c.key == "historique_vs_forme" for c in found)

    def test_injury_against_form_is_flagged(self):
        bundle = make_bundle(strong_home=True)
        bundle.news = [ds.NewsFlag("Équipe A", "Titulaire forfait", "", None,
                                   ds.Provenance("news_rss", "N", datetime.now(UTC)))]
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        found = self._detect(bundle, pred)
        assert any(c.key == "forme_vs_absences" for c in found)

    def test_bookmaker_disagreement_is_flagged(self):
        bundle = make_bundle(odds=make_odds(
            {"Équipe A": 1.8, "Draw": 3.6, "Équipe B": 4.2},
            per_book={"x": {"Équipe A": 1.35}, "y": {"Équipe A": 2.30}}))
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        found = self._detect(bundle, pred)
        assert any(c.key == "bookmakers_desaccord" for c in found)

    def test_severity_is_bounded_and_sorted(self):
        bundle = make_bundle(strong_home=True)
        bundle.news = [ds.NewsFlag("Équipe A", "Absence", "", None,
                                   ds.Provenance("news_rss", "N", datetime.now(UTC)))]
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        found = self._detect(bundle, pred)
        assert all(0.0 <= c.severity <= 1.0 for c in found)
        assert found == sorted(found, key=lambda c: -c.severity)

    def test_a_failing_pattern_detector_never_breaks_detection(self):
        class Boom:
            def detect(self, bundle, prediction):
                raise RuntimeError("modèle HS")

        bundle = make_bundle()
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        factors = FactorEngine().evaluate(bundle, pred)
        found = ContradictionDetector(Boom()).detect(
            bundle, pred, factors, MarketAnalyst().read(bundle)
        )
        assert isinstance(found, list)


# ==========================================================================
# Scénarios alternatifs
# ==========================================================================
class TestScenarios:
    def test_scenarios_come_from_the_simulation(self):
        bundle = make_bundle(strong_home=True)
        pred = engine.analyse(bundle, n_sims=20_000, seed=3)
        scenarios = ScenarioExplorer().explore(pred, pred.main_pick.key)
        assert scenarios
        assert all(0.0 <= s.probability <= 1.0 for s in scenarios)
        assert all(s.text for s in scenarios)

    def test_draw_scenario_matches_the_computed_probability(self):
        bundle = make_bundle(strong_home=True)
        pred = engine.analyse(bundle, n_sims=20_000, seed=3)
        all_scenarios = ScenarioExplorer().explore(pred, "1x2_home", limit=10)
        draw = next((s for s in all_scenarios if s.key == "nul"), None)
        if draw is not None:
            assert abs(draw.probability - pred.outcome_probs["draw"]) < 0.01

    def test_scenarios_are_ranked_by_impact(self):
        bundle = make_bundle(strong_home=True)
        pred = engine.analyse(bundle, n_sims=20_000, seed=3)
        scenarios = ScenarioExplorer().explore(pred, "1x2_home", limit=5)
        assert scenarios == sorted(scenarios, key=lambda s: -s.impact)

    def test_works_without_simulation_samples(self):
        bundle = make_bundle(strong_home=True)
        pred = engine.analyse(bundle, n_sims=10_000, seed=3)
        pred.samples = None
        scenarios = ScenarioExplorer().explore(pred, pred.main_pick.key)
        assert len(scenarios) >= 1

    def test_basket_scenarios(self):
        bundle = make_bundle(sport="basket")
        bundle.form_home = make_form("Équipe A", [118, 122, 110, 125, 119, 130],
                                     [108, 115, 104, 112, 110, 118], "basket")
        bundle.form_away = make_form("Équipe B", [106, 112, 100, 109, 104, 111],
                                     [115, 118, 112, 120, 109, 116], "basket")
        bundle.league_context = {"avg_per_team": 113.0, "estimated": True, "n": 12}
        pred = engine.analyse(bundle, n_sims=20_000, seed=4)
        scenarios = ScenarioExplorer().explore(pred, pred.main_pick.key)
        assert scenarios and all(s.probability <= 1.0 for s in scenarios)


# ==========================================================================
# Auto-évaluation
# ==========================================================================
class TestSelfAssessment:
    def test_richer_data_scores_higher(self):
        poor = make_bundle()
        poor.form_home = make_form("Équipe A", [1], [1])
        poor.form_away = make_form("Équipe B", [1], [1])
        rich = make_bundle(
            strong_home=True,
            odds=make_odds({"Équipe A": 1.8, "Draw": 3.6, "Équipe B": 4.2}),
            standings={
                "Équipe A": ds.Standing("Équipe A", 1, 30, 70, 65, 20),
                "Équipe B": ds.Standing("Équipe B", 18, 30, 25, 22, 60),
            },
            h2h=[ds.MatchResult(datetime.now(UTC), "Équipe B", True, 2, 1)],
        )
        evaluator = SelfEvaluator()
        p_poor = engine.analyse(poor, n_sims=10_000, seed=1)
        p_rich = engine.analyse(rich, n_sims=10_000, seed=1)
        assert (evaluator.assess(rich, p_rich).score
                > evaluator.assess(poor, p_poor).score)

    def test_all_criteria_are_bounded(self):
        bundle = make_bundle(strong_home=True)
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        assessment = SelfEvaluator().assess(bundle, pred)
        for value in assessment.as_dict().values():
            assert 0.0 <= value <= 1.0
        assert 0.0 <= assessment.score <= 1.0

    def test_contradictions_lower_coherence(self):
        bundle = make_bundle(strong_home=True)
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        evaluator = SelfEvaluator()
        clean = evaluator.assess(bundle, pred, contradictions=[])
        noisy = evaluator.assess(bundle, pred, contradictions=[
            Contradiction("a", "x", 0.9), Contradiction("b", "y", 0.8),
        ])
        assert noisy.source_coherence < clean.source_coherence
        assert noisy.score < clean.score

    def test_stability_is_measured_on_real_batches(self):
        bundle = make_bundle(strong_home=True)
        pred = engine.analyse(bundle, n_sims=20_000, seed=1)
        assessment = SelfEvaluator().assess(bundle, pred)
        assert 0.0 <= assessment.probability_stability <= 1.0
        # Un favori net doit être stable d'un lot de simulations à l'autre.
        assert assessment.probability_stability > 0.3

    def test_stale_data_lowers_freshness(self):
        bundle = make_bundle()
        bundle.provenances = [
            ds.Provenance("thesportsdb", "T", datetime.now(UTC) - timedelta(days=10))
        ]
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        assert SelfEvaluator().assess(bundle, pred).data_freshness < 0.4


# ==========================================================================
# Décision
# ==========================================================================
class TestDecision:
    def _decide(self, bundle, contradictions=None, assessment=None):
        pred = engine.analyse(bundle, n_sims=20_000, seed=5)
        factors = FactorEngine().evaluate(bundle, pred)
        scenarios = ScenarioExplorer().explore(
            pred, getattr(pred.main_pick, "key", None)
        )
        return DecisionMaker().decide(
            pred, factors, contradictions or [], scenarios,
            assessment or SelfAssessment(data_quality=0.8, data_quantity=0.8,
                                         data_freshness=1.0, source_coherence=1.0,
                                         probability_stability=0.9),
        ), pred

    def test_decision_is_complete(self):
        decision, _pred = self._decide(make_bundle(strong_home=True))
        assert decision.recommendation
        assert decision.market
        assert 0.0 < decision.probability <= 1.0
        assert 0.0 <= decision.confidence <= 10.0
        assert decision.rationale
        assert not decision.abstained

    def test_contradictions_reduce_confidence(self):
        bundle = make_bundle(strong_home=True)
        clean, _ = self._decide(bundle)
        noisy, _ = self._decide(bundle, contradictions=[
            Contradiction("a", "signal contraire", 1.0),
            Contradiction("b", "autre signal contraire", 1.0),
        ])
        assert noisy.confidence < clean.confidence

    def test_weak_self_assessment_reduces_confidence(self):
        bundle = make_bundle(strong_home=True)
        strong, _ = self._decide(bundle)
        weak, _ = self._decide(bundle, assessment=SelfAssessment())
        assert weak.confidence < strong.confidence

    def test_abstains_when_no_market_qualifies(self):
        pred = engine.analyse(make_bundle(), n_sims=10_000, seed=5)
        pred.main_pick = None
        decision = DecisionMaker().decide(
            pred, FactorReport(), [], [], SelfAssessment()
        )
        assert decision.abstained
        assert decision.probability == 0.0
        assert "ne permettent pas" in decision.rationale

    def test_adjustment_is_bounded_and_only_directional(self):
        """L'ajustement ne s'applique qu'aux marchés liés au vainqueur."""
        bundle = make_bundle(strong_home=True)
        pred = engine.analyse(bundle, n_sims=20_000, seed=5)
        factors = FactorEngine().evaluate(bundle, pred)
        maker = DecisionMaker()
        line = next(l for l in pred.lines if l.key == "1x2_home")
        adjusted = maker._adjust(line, factors, pred)
        assert abs(adjusted - line.prob) <= cfg.AGENT.tilt_strength + 1e-9

        total = next(l for l in pred.lines if l.key.startswith("total_over_"))
        assert maker._adjust(total, factors, pred) == total.prob

    def test_payload_is_serialisable(self):
        import json

        decision, _ = self._decide(make_bundle(strong_home=True))
        decoded = json.loads(json.dumps(decision.as_payload(), ensure_ascii=False))
        assert decoded["recommendation"]
        assert "self_assessment" in decoded


# ==========================================================================
# Agent complet : robustesse, reproductibilité, extensibilité
# ==========================================================================
class TestAgent:
    def test_full_pipeline_runs_every_step(self, ledger):
        bundle = make_bundle(strong_home=True,
                             odds=make_odds({"Équipe A": 1.7, "Draw": 3.8, "Équipe B": 4.8}))
        agent = AnalysisAgent(_Hub(bundle), ledger=ledger)
        result = agent.analyse_match(comp(), "Équipe A", "Équipe B")
        for step in ("collecte", "validation", "simulation", "marché", "facteurs",
                     "contradictions", "scénarios", "auto-évaluation", "décision"):
            assert step in result.steps
        assert result.decision.recommendation
        assert result.duration_s >= 0

    def test_survives_total_collection_failure(self, ledger):
        """Toutes les sources tombent : l'agent produit quand même un résultat."""
        agent = AnalysisAgent(_Hub(boom=True), ledger=ledger)
        result = agent.analyse_match(comp(), "Arsenal", "Chelsea")
        assert result.decision is not None
        assert result.decision.confidence <= 4.0
        assert "collecte (partielle)" in result.steps

    def test_missing_odds_do_not_stop_the_analysis(self, ledger):
        agent = AnalysisAgent(_Hub(make_bundle(strong_home=True)), ledger=ledger)
        result = agent.analyse_match(comp(), "Équipe A", "Équipe B")
        assert not result.market.available
        assert result.decision.recommendation
        assert ("cotes", "aucune cote publiée") in result.validation.discarded

    def test_same_data_yields_the_same_analysis(self, ledger):
        """Reproductibilité : deux exécutions sur les mêmes données concordent."""
        agent = AnalysisAgent(_Hub(make_bundle(strong_home=True)), ledger=ledger)
        first = agent.analyse_match(comp(), "Équipe A", "Équipe B", record=False)
        second = agent.analyse_match(comp(), "Équipe A", "Équipe B", record=False)
        assert first.decision.fingerprint == second.decision.fingerprint
        assert first.decision.probability == second.decision.probability
        assert first.decision.recommendation == second.decision.recommendation

    def test_different_data_yields_a_different_fingerprint(self, ledger):
        agent_a = AnalysisAgent(_Hub(make_bundle(strong_home=True)), ledger=ledger)
        agent_b = AnalysisAgent(_Hub(make_bundle(strong_home=False)), ledger=ledger)
        first = agent_a.analyse_match(comp(), "Équipe A", "Équipe B", record=False)
        second = agent_b.analyse_match(comp(), "Équipe A", "Équipe B", record=False)
        assert first.decision.fingerprint != second.decision.fingerprint

    def test_analysis_is_recorded_in_the_ledger(self, ledger):
        agent = AnalysisAgent(_Hub(make_bundle(strong_home=True)), ledger=ledger)
        agent.analyse_match(comp(), "Équipe A", "Équipe B")
        entries = ledger.all()
        assert len(entries) == 1
        assert entries[0].recommendation
        assert not entries[0].resolved

    def test_payload_shape_for_a_future_api(self, ledger):
        import json

        agent = AnalysisAgent(_Hub(make_bundle(strong_home=True)), ledger=ledger)
        payload = agent.analyse_match(comp(), "Équipe A", "Équipe B").as_payload()
        decoded = json.loads(json.dumps(payload, ensure_ascii=False))
        assert set(decoded) >= {"match", "decision", "probabilities", "market", "data"}

    # -- points d'insertion pour un modèle d'IA -------------------------
    def test_a_custom_score_model_can_replace_the_engine(self, ledger):
        calls = []

        class Custom:
            def predict(self, bundle, seed=None):
                calls.append(seed)
                return engine.analyse(bundle, n_sims=10_000, seed=seed)

        agent = AnalysisAgent(_Hub(make_bundle(strong_home=True)),
                              score_model=Custom(), ledger=ledger)
        result = agent.analyse_match(comp(), "Équipe A", "Équipe B", record=False)
        assert calls, "le modèle injecté doit être appelé"
        assert "simulation (modèle externe)" in result.steps

    def test_a_broken_score_model_falls_back_to_the_engine(self, ledger):
        class Broken:
            def predict(self, bundle, seed=None):
                raise RuntimeError("modèle indisponible")

        agent = AnalysisAgent(_Hub(make_bundle(strong_home=True)),
                              score_model=Broken(), ledger=ledger)
        result = agent.analyse_match(comp(), "Équipe A", "Équipe B", record=False)
        assert "simulation" in result.steps
        assert result.decision.recommendation

    def test_a_custom_narrator_is_used(self, ledger):
        class Narrator:
            def write(self, decision, prediction):
                return "Justification personnalisée."

        agent = AnalysisAgent(_Hub(make_bundle(strong_home=True)),
                              narrator=Narrator(), ledger=ledger)
        result = agent.analyse_match(comp(), "Équipe A", "Équipe B", record=False)
        assert result.decision.rationale == "Justification personnalisée."

    def test_a_custom_weighting_policy_is_used(self, ledger):
        class Policy:
            def weights(self, context):
                return {spec.key: 0.0 for spec in cfg.FACTOR_WEIGHTS}

        agent = AnalysisAgent(_Hub(make_bundle(strong_home=True)),
                              weighting_policy=Policy(), ledger=ledger)
        result = agent.analyse_match(comp(), "Équipe A", "Équipe B", record=False)
        assert all(f.weight == 0.0 for f in result.factors.factors)
        assert result.factors.tilt == 0.0

    def test_default_implementations_satisfy_the_protocols(self):
        from agent.decision import TemplateNarrator
        from agent.contracts import ConfiguredWeights

        assert isinstance(TemplateNarrator(), NarrativeWriter)
        assert isinstance(ConfiguredWeights(), WeightingPolicy)
