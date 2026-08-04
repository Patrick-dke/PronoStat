"""Tests de la mémoire de l'agent : journal, calibration, propositions.

Le point le plus important vérifié ici : **aucun réglage n'est jamais appliqué
automatiquement**. Une proposition acceptée est seulement enregistrée.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
import agent.memory as mem  # noqa: E402
from agent.memory import (  # noqa: E402
    LedgerEntry,
    PerformanceAnalyst,
    PerformanceReport,
    PredictionLedger,
    TuningAdvisor,
    evaluate_market,
)

UTC = timezone.utc


def entry(probability=0.6, hit=None, market="1x2_home", home="A", away="B") -> LedgerEntry:
    return LedgerEntry(
        id=f"football|PL|{home}|{away}",
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        sport="football", competition="Premier League",
        home=home, away=away, market_key=market,
        recommendation="Victoire A", probability=probability, confidence=7.0,
        hit=hit,
    )


# ==========================================================================
# Évaluation d'un marché face au score réel
# ==========================================================================
class TestMarketEvaluation:
    @pytest.mark.parametrize(
        "market,home,away,expected",
        [
            ("1x2_home", 2, 1, True), ("1x2_home", 1, 2, False),
            ("1x2_away", 0, 3, True), ("1x2_draw", 1, 1, True),
            ("dc_1x", 1, 1, True), ("dc_1x", 0, 1, False),
            ("dc_x2", 0, 0, True), ("dc_12", 1, 1, False),
            ("btts_yes", 2, 1, True), ("btts_yes", 2, 0, False),
            ("btts_no", 3, 0, True),
            ("total_over_2.5", 2, 1, True), ("total_over_2.5", 1, 1, False),
            ("total_under_2.5", 1, 1, True),
            ("hcp_home_-1.5", 3, 1, True), ("hcp_home_-1.5", 2, 1, False),
        ],
    )
    def test_known_markets(self, market, home, away, expected):
        assert evaluate_market(market, home, away) is expected

    def test_unresolvable_markets_stay_unknown(self):
        """Mieux vaut ne pas conclure que produire un taux de réussite faux."""
        for market in ("ml_home", "puckline_home_-1.5", "sets_2-1", "", "inconnu"):
            assert evaluate_market(market, 3, 2) is None


# ==========================================================================
# Journal
# ==========================================================================
class TestLedger:
    @pytest.fixture
    def ledger(self, tmp_path):
        return PredictionLedger(path=tmp_path / "ledger.json")

    def test_resolve_marks_a_hit(self, ledger, tmp_path):
        rows = [entry(market="1x2_home").__dict__]
        (tmp_path / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")
        assert ledger.resolve("football|PL|A|B", 2, 0)
        resolved = ledger.resolved()
        assert len(resolved) == 1 and resolved[0].hit is True
        assert resolved[0].actual_home == 2

    def test_resolve_marks_a_miss(self, ledger, tmp_path):
        rows = [entry(market="1x2_home").__dict__]
        (tmp_path / "ledger.json").write_text(json.dumps(rows), encoding="utf-8")
        ledger.resolve("football|PL|A|B", 0, 3)
        assert ledger.resolved()[0].hit is False

    def test_unknown_id_changes_nothing(self, ledger, tmp_path):
        (tmp_path / "ledger.json").write_text(
            json.dumps([entry().__dict__]), encoding="utf-8"
        )
        assert not ledger.resolve("autre|match", 1, 0)
        assert ledger.pending()

    def test_empty_ledger_is_safe(self, ledger):
        assert ledger.all() == [] and ledger.pending() == []


# ==========================================================================
# Calibration
# ==========================================================================
class TestPerformance:
    def test_no_data_yields_an_empty_report(self):
        report = PerformanceAnalyst().report([])
        assert report.resolved == 0
        assert report.hit_rate is None and report.brier is None
        assert not report.is_meaningful

    def test_perfect_forecaster_has_a_low_brier_score(self):
        entries = [entry(0.95, hit=True) for _ in range(20)]
        report = PerformanceAnalyst().report(entries)
        assert report.brier < 0.02
        assert report.hit_rate == 1.0

    def test_overconfident_forecaster_is_detected(self):
        """Annoncer 80 % et réussir 40 % du temps doit ressortir comme un biais."""
        entries = [entry(0.80, hit=(i % 5 < 2)) for i in range(25)]
        report = PerformanceAnalyst().report(entries)
        assert report.bias is not None and report.bias < -0.3
        assert report.brier > 0.2

    def test_calibration_bins_are_populated(self):
        entries = (
            [entry(0.50, hit=(i % 2 == 0)) for i in range(10)]
            + [entry(0.70, hit=(i % 10 < 7)) for i in range(10)]
        )
        report = PerformanceAnalyst().report(entries)
        assert len(report.bins) >= 2
        for b in report.bins:
            assert 0.0 <= b.observed <= 1.0 and b.count > 0

    def test_results_are_split_by_market_family(self):
        entries = (
            [entry(0.6, hit=True, market="1x2_home") for _ in range(6)]
            + [entry(0.6, hit=False, market="total_over_2.5") for _ in range(4)]
        )
        report = PerformanceAnalyst().report(entries)
        assert report.by_market["1x2"] == (6, 6)
        assert report.by_market["total"] == (4, 0)

    def test_meaningfulness_threshold(self):
        assert not PerformanceAnalyst().report(
            [entry(0.6, hit=True) for _ in range(19)]
        ).is_meaningful
        assert PerformanceAnalyst().report(
            [entry(0.6, hit=True) for _ in range(20)]
        ).is_meaningful


# ==========================================================================
# Propositions de réglage — jamais appliquées d'office
# ==========================================================================
class TestTuningAdvisor:
    @pytest.fixture
    def advisor(self, tmp_path):
        return TuningAdvisor(
            path=tmp_path / "proposals.json",
            overrides_path=tmp_path / "overrides.json",
        )

    def test_no_suggestion_without_enough_evidence(self, advisor):
        report = PerformanceAnalyst().report([entry(0.8, hit=False) for _ in range(10)])
        assert advisor.suggest(report) == []

    def test_overconfidence_suggests_trusting_the_market_more(self, advisor):
        report = PerformanceAnalyst().report(
            [entry(0.80, hit=(i % 5 < 2)) for i in range(25)]
        )
        proposals = advisor.suggest(report)
        market = [p for p in proposals if p.parameter == "MARKET_WEIGHT"]
        assert market and market[0].proposed > market[0].current
        assert market[0].evidence

    def test_underconfidence_suggests_the_opposite(self, advisor):
        report = PerformanceAnalyst().report(
            [entry(0.50, hit=(i % 10 < 9)) for i in range(25)]
        )
        proposals = advisor.suggest(report)
        market = [p for p in proposals if p.parameter == "MARKET_WEIGHT"]
        assert market and market[0].proposed < market[0].current

    def test_proposals_are_registered_once_per_parameter(self, advisor):
        report = PerformanceAnalyst().report(
            [entry(0.80, hit=(i % 5 < 2)) for i in range(25)]
        )
        first = advisor.register(advisor.suggest(report))
        second = advisor.register(advisor.suggest(report))
        assert first and not second
        assert len(advisor.pending()) == len(first)

    def test_accepting_never_changes_the_running_configuration(self, advisor):
        """Le cœur de l'exigence : accepter n'applique rien tout seul."""
        before = cfg.ENGINE.market_weight
        report = PerformanceAnalyst().report(
            [entry(0.80, hit=(i % 5 < 2)) for i in range(25)]
        )
        proposals = advisor.register(advisor.suggest(report))
        target = next(p for p in proposals if p.parameter == "MARKET_WEIGHT")
        assert advisor.decide(target.id, accept=True)

        assert cfg.ENGINE.market_weight == before      # rien n'a bougé
        assert advisor.accepted_overrides()["MARKET_WEIGHT"] == target.proposed
        assert target.id not in {p.id for p in advisor.pending()}

    def test_rejecting_records_nothing(self, advisor):
        report = PerformanceAnalyst().report(
            [entry(0.80, hit=(i % 5 < 2)) for i in range(25)]
        )
        proposals = advisor.register(advisor.suggest(report))
        for proposal in proposals:
            assert advisor.decide(proposal.id, accept=False)
        assert advisor.accepted_overrides() == {}
        assert not advisor.pending()

    def test_deciding_twice_is_refused(self, advisor):
        report = PerformanceAnalyst().report(
            [entry(0.80, hit=(i % 5 < 2)) for i in range(25)]
        )
        proposals = advisor.register(advisor.suggest(report))
        assert advisor.decide(proposals[0].id, accept=True)
        assert not advisor.decide(proposals[0].id, accept=True)

    def test_unknown_proposal_is_refused(self, advisor):
        assert not advisor.decide("inexistante", accept=True)

    def test_override_file_states_it_is_not_applied(self, advisor, tmp_path):
        report = PerformanceAnalyst().report(
            [entry(0.80, hit=(i % 5 < 2)) for i in range(25)]
        )
        proposals = advisor.register(advisor.suggest(report))
        advisor.decide(proposals[0].id, accept=True)
        data = json.loads((tmp_path / "overrides.json").read_text(encoding="utf-8"))
        assert "automatiquement" in data["_note"]


class TestResolvePending:
    """Sans confrontation aux résultats réels, chaque analyse resterait
    « en attente » à jamais et le taux de réussite ne se formerait jamais."""

    class _Hub:
        def __init__(self, score):
            self.score, self.appels = score, []

        def final_score(self, comp, home, away):
            self.appels.append((home, away))
            return self.score

    @staticmethod
    def _ledger_avec_entree(tmp_path, heures_ecoulees: float):
        from datetime import datetime, timedelta, timezone
        led = mem.PredictionLedger(path=tmp_path / "l.json")
        quand = datetime.now(timezone.utc) - timedelta(hours=heures_ecoulees)
        led._save([{
            "id": "e1", "created_at": quand.isoformat(timespec="seconds"),
            "sport": "football", "competition": "Premier League",
            "home": "Arsenal", "away": "Chelsea",
            "market_key": "1x2_home", "recommendation": "Arsenal gagne",
            "probability": 0.6, "confidence": 7.0,
        }])
        return led

    def test_a_played_match_is_resolved(self, tmp_path):
        led = self._ledger_avec_entree(tmp_path, heures_ecoulees=48)
        hub = self._Hub((2, 0))
        assert mem.resolve_pending(led, hub) == 1
        entree = led.all()[0]
        assert entree.resolved and entree.hit is True
        assert (entree.actual_home, entree.actual_away) == (2, 0)

    def test_a_too_recent_match_is_left_alone(self, tmp_path):
        """Un score n'est publié qu'après le coup de sifflet final."""
        led = self._ledger_avec_entree(tmp_path, heures_ecoulees=1)
        hub = self._Hub((2, 0))
        assert mem.resolve_pending(led, hub) == 0
        assert hub.appels == [], "aucune recherche ne doit partir trop tôt"
        assert not led.all()[0].resolved

    def test_an_unknown_score_leaves_it_pending(self, tmp_path):
        """Introuvable n'est pas un échec : on n'invente rien."""
        led = self._ledger_avec_entree(tmp_path, heures_ecoulees=48)
        assert mem.resolve_pending(led, self._Hub(None)) == 0
        assert not led.all()[0].resolved
        assert led.all()[0].hit is None

    def test_a_failing_source_does_not_crash(self, tmp_path):
        class Casse:
            def final_score(self, *_a):
                raise RuntimeError("panne")

        led = self._ledger_avec_entree(tmp_path, heures_ecoulees=48)
        assert mem.resolve_pending(led, Casse()) == 0
