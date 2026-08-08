"""Tests unitaires du moteur : no-vig, Monte Carlo, signaux enrichis."""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
import engine  # noqa: E402
from data_sources import (  # noqa: E402
    Bundle,
    MatchResult,
    OddsSnapshot,
    Provenance,
    Standing,
    TeamForm,
)

UTC = timezone.utc


# ==========================================================================
# Fixtures : données factices (aucun appel réseau dans les tests)
# ==========================================================================
def comp_for(sport: str):
    return cfg.competitions(sport)[0]


def make_form(
    team: str,
    sport: str,
    scored: list[float],
    conceded: list[float],
    alternate_home: bool = True,
    extras: list[dict] | None = None,
) -> TeamForm:
    now = datetime.now(UTC)
    matches = []
    for i, (s, c) in enumerate(zip(scored, conceded)):
        extra = dict(extras[i]) if extras and i < len(extras) else {}
        matches.append(
            MatchResult(
                # 3 jours de repos = valeur de référence → aucun effet fatigue.
                date=now - timedelta(days=3 + 7 * i),
                opponent=f"Adversaire {i}",
                home=(i % 2 == 0) if alternate_home else True,
                scored=s,
                conceded=c,
                extra=extra,
            )
        )
    return TeamForm(
        team=team,
        sport=sport,
        matches=matches,
        provenance=Provenance("test", "Test", datetime.now(UTC)),
    )


def make_bundle(sport, home_stats, away_stats, odds=None, league_avg=None, comp=None) -> Bundle:
    comp = comp or comp_for(sport)
    bundle = Bundle(sport=sport, home="Équipe A", away="Équipe B", competition=comp)
    bundle.form_home = make_form("Équipe A", sport, *home_stats)
    bundle.form_away = make_form("Équipe B", sport, *away_stats)
    bundle.odds = odds
    samples = [
        (m.scored + m.conceded) / 2
        for f in (bundle.form_home, bundle.form_away)
        for m in f.matches
    ]
    bundle.league_context = {
        "avg_per_team": league_avg if league_avg else sum(samples) / len(samples),
        "estimated": True,
        "n": len(samples),
    }
    bundle.provenances = [Provenance("test", "Test", datetime.now(UTC))]
    return bundle


def make_odds(h2h: dict, books: int = 8, home="Équipe A", away="Équipe B") -> OddsSnapshot:
    return OddsSnapshot(
        home_team=home,
        away_team=away,
        commence_time=None,
        sport_key="test_key",
        provenance=Provenance("odds", "Odds", datetime.now(UTC)),
        h2h=h2h,
        bookmaker_count=books,
    )


FOOT_HOME = ([2, 3, 1, 2, 2, 1, 3, 2, 1, 2], [0, 1, 1, 0, 2, 1, 1, 0, 1, 1])
FOOT_AWAY = ([1, 0, 1, 2, 0, 1, 1, 0, 2, 1], [2, 1, 1, 1, 3, 2, 0, 2, 1, 2])


# ==========================================================================
# 1. NO-VIG
# ==========================================================================
class TestNoVig:
    def test_probabilities_sum_to_one_three_way(self):
        probs = engine.remove_vig({"home": 2.10, "draw": 3.40, "away": 3.80})
        assert probs is not None
        assert math.isclose(sum(probs.values()), 1.0, abs_tol=1e-12)

    def test_probabilities_sum_to_one_two_way(self):
        probs = engine.remove_vig({"home": 1.55, "away": 2.55})
        assert math.isclose(sum(probs.values()), 1.0, abs_tol=1e-12)

    @pytest.mark.parametrize(
        "odds",
        [
            {"home": 1.20, "draw": 7.50, "away": 15.0},
            {"home": 4.33, "draw": 3.60, "away": 1.85},
            {"home": 1.01, "away": 21.0},
            {"a": 2.0, "b": 2.0, "c": 4.0, "d": 8.0},
        ],
    )
    def test_sum_to_one_various_books(self, odds):
        probs = engine.remove_vig(odds)
        assert probs is not None
        assert math.isclose(sum(probs.values()), 1.0, abs_tol=1e-12)
        assert all(0.0 < p < 1.0 for p in probs.values())

    def test_power_method_also_sums_to_one(self):
        probs = engine.remove_vig({"home": 2.10, "draw": 3.40, "away": 3.80}, method="power")
        assert math.isclose(sum(probs.values()), 1.0, abs_tol=1e-6)

    def test_novig_is_below_raw_implied(self):
        odds = {"home": 2.10, "draw": 3.40, "away": 3.80}
        raw = engine.implied_probabilities(odds)
        fair = engine.remove_vig(odds)
        assert sum(raw.values()) > 1.0  # la marge existe bien
        for key in raw:
            assert fair[key] < raw[key]

    def test_order_is_preserved(self):
        fair = engine.remove_vig({"home": 1.60, "draw": 4.00, "away": 5.50})
        assert fair["home"] > fair["draw"] > fair["away"]

    def test_margin_is_positive(self):
        margin = engine.book_margin({"home": 2.10, "draw": 3.40, "away": 3.80})
        assert margin is not None and 0 < margin < 0.30

    def test_fair_book_has_zero_margin(self):
        odds = {"home": 3.0, "draw": 3.0, "away": 3.0}
        assert math.isclose(engine.book_margin(odds), 0.0, abs_tol=1e-12)
        fair = engine.remove_vig(odds)
        assert all(math.isclose(p, 1 / 3, abs_tol=1e-12) for p in fair.values())

    def test_insufficient_data_returns_none(self):
        assert engine.remove_vig({}) is None
        assert engine.remove_vig({"home": 1.80}) is None
        assert engine.remove_vig({"home": 0.5, "away": 0.9}) is None  # cotes invalides

    def test_unknown_method_raises(self):
        with pytest.raises(ValueError):
            engine.remove_vig({"home": 2.0, "away": 2.0}, method="magie")

    def test_map_h2h_labels(self):
        mapped = engine.map_h2h_odds(
            {"Real Madrid": 1.75, "Draw": 3.90, "FC Barcelona": 4.20},
            "Real Madrid CF",
            "Barcelona",
        )
        assert mapped == {"home": 1.75, "draw": 3.90, "away": 4.20}


# ==========================================================================
# 2. Distributions & Monte Carlo
# ==========================================================================
class TestDistributions:
    def test_poisson_pmf_sums_to_one(self):
        assert math.isclose(float(engine.poisson_pmf(np.arange(0, 40), 2.3).sum()), 1.0,
                            rel_tol=1e-9)

    def test_poisson_mean(self):
        grid = np.arange(0, 60)
        pmf = engine.poisson_pmf(grid, 3.1)
        assert math.isclose(float((grid * pmf).sum()), 3.1, rel_tol=1e-6)

    def test_dixon_coles_matrix_is_a_distribution(self):
        matrix = engine.dixon_coles_matrix(1.6, 1.1)
        assert math.isclose(float(matrix.sum()), 1.0, rel_tol=1e-12)
        assert (matrix > 0).all()

    def test_dixon_coles_boosts_low_scores(self):
        independent = engine.dixon_coles_matrix(1.3, 1.1, rho=0.0)
        adjusted = engine.dixon_coles_matrix(1.3, 1.1, rho=-0.10)
        assert adjusted[0, 0] > independent[0, 0]
        assert adjusted[1, 1] > independent[1, 1]
        assert adjusted[0, 1] < independent[0, 1]

    def test_outcome_probs_sum_to_one(self):
        home, draw, away = engine.matrix_outcome_probs(engine.dixon_coles_matrix(1.9, 0.9))
        assert math.isclose(home + draw + away, 1.0, rel_tol=1e-10)
        assert home > away

    def test_norm_cdf_ppf_roundtrip(self):
        for p in (0.05, 0.25, 0.5, 0.83, 0.99):
            assert math.isclose(engine.norm_cdf(engine.norm_ppf(p)), p, abs_tol=1e-6)


class TestMonteCarlo:
    """Le Monte Carlo doit converger vers la distribution théorique."""

    def test_sample_scores_reproduce_matrix(self):
        matrix = engine.dixon_coles_matrix(1.7, 1.2)
        home, away = engine.sample_scores(matrix, 60_000, np.random.default_rng(42))
        exp_home, exp_draw, exp_away = engine.matrix_outcome_probs(matrix)
        diff = home - away
        assert abs(float(np.mean(diff > 0)) - exp_home) < 0.012
        assert abs(float(np.mean(diff == 0)) - exp_draw) < 0.012
        assert abs(float(np.mean(diff < 0)) - exp_away) < 0.012

    def test_sample_means_match_lambdas(self):
        matrix = engine.dixon_coles_matrix(2.0, 1.4, rho=0.0)
        home, away = engine.sample_scores(matrix, 80_000, np.random.default_rng(7))
        assert abs(float(np.mean(home)) - 2.0) < 0.03
        assert abs(float(np.mean(away)) - 1.4) < 0.03

    def test_sampling_is_reproducible_with_seed(self):
        matrix = engine.dixon_coles_matrix(1.5, 1.5)
        a = engine.sample_scores(matrix, 5_000, np.random.default_rng(1))
        b = engine.sample_scores(matrix, 5_000, np.random.default_rng(1))
        assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])

    def test_two_independent_runs_are_close(self):
        matrix = engine.dixon_coles_matrix(1.4, 1.2)
        h1, a1 = engine.sample_scores(matrix, 20_000, np.random.default_rng(11))
        h2, a2 = engine.sample_scores(matrix, 20_000, np.random.default_rng(99))
        assert abs(float(np.mean(h1 > a1)) - float(np.mean(h2 > a2))) < 0.02


# ==========================================================================
# 3. Calibration
# ==========================================================================
class TestCalibration:
    def test_blend_respects_weight(self):
        blended = engine.blend_probs(
            {"home": 0.50, "draw": 0.30, "away": 0.20},
            {"home": 0.30, "draw": 0.30, "away": 0.40},
            weight=0.60,
        )
        assert math.isclose(blended["home"], 0.6 * 0.50 + 0.4 * 0.30, abs_tol=1e-9)
        assert math.isclose(sum(blended.values()), 1.0, abs_tol=1e-9)

    def test_blend_without_market_returns_model(self):
        model = {"home": 0.4, "draw": 0.3, "away": 0.3}
        assert engine.blend_probs(None, model) == model

    def test_nelder_mead_finds_minimum(self):
        def rosenbrock(x):
            return (1 - x[0]) ** 2 + 100 * (x[1] - x[0] ** 2) ** 2

        best = engine.nelder_mead(rosenbrock, [-1.2, 1.0], step=0.3, max_iter=2000)
        assert abs(best[0] - 1.0) < 0.02 and abs(best[1] - 1.0) < 0.05

    def test_calibration_matches_target(self):
        target = {"home": 0.55, "draw": 0.25, "away": 0.20}
        lam_h, lam_a, residual = engine.calibrate_lambdas(1.4, 1.3, target)
        home, draw, away = engine.matrix_outcome_probs(engine.dixon_coles_matrix(lam_h, lam_a))
        assert residual < 0.01
        assert abs(home - target["home"]) < 0.05
        assert abs(away - target["away"]) < 0.05


# ==========================================================================
# 4. Signaux enrichis (§9 de la révision)
# ==========================================================================
class TestEnrichedSignals:
    def test_xg_shifts_strength(self):
        """Une équipe qui surperforme ses xG voit sa force corrigée à la baisse."""
        lucky = make_form(
            "Chanceux", "football",
            [3, 3, 3, 3, 3, 3], [1, 1, 1, 1, 1, 1],
            extras=[{"xg_for": 1.0, "xg_against": 1.0}] * 6,
        )
        honest = make_form(
            "Sérieux", "football",
            [3, 3, 3, 3, 3, 3], [1, 1, 1, 1, 1, 1],
        )
        s_lucky = engine.compute_strength(lucky, 1.4, at_home=True)
        s_honest = engine.compute_strength(honest, 1.4, at_home=True)
        assert s_lucky.attack < s_honest.attack
        assert any("xG" in sig for sig in s_lucky.signals)
        assert not any("xG" in sig for sig in s_honest.signals)

    def test_xg_ignored_when_sample_too_small(self):
        form = make_form(
            "X", "football", [2, 2, 2], [1, 1, 1],
            extras=[{"xg_for": 0.5, "xg_against": 0.5}, {}, {}],
        )
        strength = engine.compute_strength(form, 1.4, at_home=True)
        assert not any("xG" in sig for sig in strength.signals)

    def test_standings_widen_the_sample(self):
        form = make_form("X", "football", [3, 3, 3, 3], [0, 0, 0, 0])
        standing = Standing(team="X", rank=12, played=30, points=35,
                            goals_for=30, goals_against=40)
        without = engine.compute_strength(form, 1.4, at_home=True)
        with_table = engine.compute_strength(form, 1.4, at_home=True, standing=standing)
        # La saison complète (1 but/match) tempère une forme flatteuse.
        assert with_table.attack < without.attack
        assert any("saison" in sig for sig in with_table.signals)

    def test_standings_ignored_when_too_few_games(self):
        form = make_form("X", "football", [2, 2, 2, 2], [1, 1, 1, 1])
        standing = Standing(team="X", rank=1, played=2, points=6, goals_for=8, goals_against=0)
        strength = engine.compute_strength(form, 1.4, at_home=True, standing=standing)
        assert not any("saison" in sig for sig in strength.signals)

    def test_h2h_tilt_direction_and_damping(self):
        now = datetime.now(UTC)
        dominant = [MatchResult(now, "B", True, 3, 0) for _ in range(6)]
        weak = [MatchResult(now, "B", True, 0, 3) for _ in range(6)]
        assert engine.h2h_tilt(dominant) > 0
        assert engine.h2h_tilt(weak) < 0
        assert engine.h2h_tilt([]) == 0.0
        # Deux matchs pèsent nettement moins que six.
        assert abs(engine.h2h_tilt(dominant[:2])) < abs(engine.h2h_tilt(dominant))
        # L'effet reste borné par la configuration.
        assert abs(engine.h2h_tilt(dominant)) <= cfg.ENGINE.h2h_weight + 1e-9

    def test_rest_multiplier_bounds(self):
        now = datetime.now(UTC)
        tired = TeamForm("T", "basket", [MatchResult(now, "X", True, 100, 90)],
                         Provenance("t", "T", now))
        rested = TeamForm(
            "R", "basket",
            [MatchResult(now - timedelta(days=6), "X", True, 100, 90)],
            Provenance("t", "T", now),
        )
        assert engine.rest_multiplier(tired) < 1.0
        assert engine.rest_multiplier(tired) >= 1.0 - cfg.ENGINE.rest_penalty_max
        assert engine.rest_multiplier(rested) == 1.0
        assert engine.rest_multiplier(None) == 1.0

    def test_profile_reports_only_real_data(self):
        form = make_form("X", "football", [2, 1, 0, 3], [1, 1, 0, 2])
        profile = engine.build_profile(form, None, over_line=2.5)
        assert profile.matches == 4
        assert profile.form_string == "WDDW"
        assert profile.clean_sheet_rate == pytest.approx(0.25)
        assert profile.btts_rate == pytest.approx(0.75)
        assert profile.xg_for is None          # aucune source ne les a fournis
        assert profile.corners_for is None
        assert "xg_for" not in profile.available

    def test_profile_includes_stats_when_provided(self):
        form = make_form(
            "X", "football", [2, 1, 2, 1], [1, 1, 0, 2],
            extras=[{"xg_for": 1.8, "xg_against": 1.1, "corners_for": 6.0,
                     "possession": 55.0, "shots": 14.0}] * 4,
        )
        profile = engine.build_profile(form, Standing("X", 3, 20, 40, 35, 20), 2.5)
        assert profile.xg_for == pytest.approx(1.8)
        assert profile.possession == pytest.approx(55.0)
        assert profile.rank == 3
        assert profile.points_per_game == pytest.approx(2.0)


# ==========================================================================
# 5. Simulations complètes par sport
# ==========================================================================
class TestFootball:
    @pytest.fixture
    def bundle(self):
        return make_bundle("football", FOOT_HOME, FOOT_AWAY)

    def test_probabilities_sum_to_one(self, bundle):
        pred = engine.simulate_goal_sport(bundle, "football", n_sims=10_000, seed=3)
        assert math.isclose(sum(pred.outcome_probs.values()), 1.0, abs_tol=1e-9)
        assert set(pred.outcome_probs) == {"home", "draw", "away"}

    def test_minimum_simulation_count(self, bundle):
        assert engine.simulate_goal_sport(bundle, "football").n_sims >= 10_000

    def test_stronger_team_is_favourite(self, bundle):
        pred = engine.simulate_goal_sport(bundle, "football", n_sims=20_000, seed=5)
        assert pred.outcome_probs["home"] > pred.outcome_probs["away"]
        assert pred.favorite == bundle.home

    def test_top_scores_are_sorted_and_valid(self, bundle):
        pred = engine.simulate_goal_sport(bundle, "football", n_sims=20_000, seed=5)
        probs = [p for _s, p in pred.top_scores]
        assert probs == sorted(probs, reverse=True)
        assert 0 < probs[0] < 1
        for label, _p in pred.top_scores:
            h, a = label.split("-")
            assert h.isdigit() and a.isdigit()

    def test_derived_markets_are_coherent(self, bundle):
        pred = engine.simulate_goal_sport(bundle, "football", n_sims=20_000, seed=5)
        o15 = pred.line("total_over_1.5").prob
        o25 = pred.line("total_over_2.5").prob
        o35 = pred.line("total_over_3.5").prob
        assert o15 > o25 > o35  # monotonie des seuils
        assert math.isclose(o25 + pred.line("total_under_2.5").prob, 1.0, abs_tol=1e-9)
        assert math.isclose(
            pred.line("btts_yes").prob + pred.line("btts_no").prob, 1.0, abs_tol=1e-9
        )
        assert pred.line("dc_1x").prob >= pred.outcome_probs["home"]

    def test_market_anchoring_moves_probabilities(self):
        odds = make_odds({"Équipe A": 5.00, "Draw": 4.00, "Équipe B": 1.65})
        free = engine.simulate_goal_sport(
            make_bundle("football", FOOT_HOME, FOOT_AWAY), "football", n_sims=20_000, seed=1
        )
        anchored = engine.simulate_goal_sport(
            make_bundle("football", FOOT_HOME, FOOT_AWAY, odds=odds),
            "football", n_sims=20_000, seed=1,
        )
        assert anchored.outcome_probs["away"] > free.outcome_probs["away"] + 0.10
        assert math.isclose(sum(anchored.market_probs.values()), 1.0, abs_tol=1e-9)

    def test_corners_absent_when_no_data(self, bundle):
        pred = engine.simulate_goal_sport(bundle, "football", n_sims=10_000, seed=2)
        assert "corners" in pred.unavailable
        assert not pred.lines_group("corners_over_")

    def test_corners_present_when_api_provides_them(self):
        bundle = make_bundle(
            "football", ([2, 1, 2, 1, 2], [1, 1, 0, 1, 1]), ([1, 1, 0, 2, 1], [1, 2, 1, 1, 2])
        )
        for form in (bundle.form_home, bundle.form_away):
            for m in form.matches:
                m.extra.update({"corners_for": 5.0, "corners_against": 4.0})
        pred = engine.simulate_goal_sport(bundle, "football", n_sims=10_000, seed=2)
        assert "corners" not in pred.unavailable
        assert math.isclose(pred.expected["corners_total"], 9.0, abs_tol=1e-9)
        assert all(0 <= l.prob <= 1 for l in pred.lines_group("corners_over_"))


class TestHockey:
    @pytest.fixture
    def bundle(self):
        return make_bundle(
            "hockey",
            ([3, 4, 2, 3, 5, 2, 3, 4, 1, 3], [2, 1, 3, 2, 2, 4, 1, 2, 3, 2]),
            ([2, 3, 1, 2, 2, 3, 1, 2, 4, 2], [3, 2, 4, 3, 1, 2, 3, 3, 2, 3]),
        )

    def test_overtime_and_puckline(self, bundle):
        pred = engine.simulate_goal_sport(bundle, "hockey", n_sims=20_000, seed=8)
        ml_home, ml_away = pred.line("ml_home").prob, pred.line("ml_away").prob
        assert math.isclose(ml_home + ml_away, 1.0, abs_tol=0.02)
        assert ml_home > pred.outcome_probs["home"]  # la prolongation ajoute des victoires
        assert 0 < pred.expected["p_overtime"] < 0.5
        assert pred.line("puckline_home_-1.5").prob < ml_home
        assert pred.line("puckline_home_+1.5").prob > ml_home

    def test_hockey_totals_use_hockey_lines(self, bundle):
        pred = engine.simulate_goal_sport(bundle, "hockey", n_sims=10_000, seed=8)
        assert pred.line("total_over_5.5") is not None
        assert pred.line("total_over_2.5") is None


class TestBasket:
    @pytest.fixture
    def bundle(self):
        return make_bundle(
            "basket",
            ([118, 122, 110, 125, 119, 130, 112, 121, 116, 124],
             [108, 115, 104, 112, 110, 118, 106, 109, 111, 113]),
            ([106, 112, 100, 109, 104, 111, 98, 107, 103, 110],
             [115, 118, 112, 120, 109, 116, 114, 111, 117, 113]),
            league_avg=113.0,
        )

    def test_no_draw_and_probabilities_sum_to_one(self, bundle):
        pred = engine.simulate_basket(bundle, n_sims=20_000, seed=4)
        assert "draw" not in pred.outcome_probs
        assert math.isclose(sum(pred.outcome_probs.values()), 1.0, abs_tol=1e-9)

    def test_expected_values_are_realistic(self, bundle):
        pred = engine.simulate_basket(bundle, n_sims=20_000, seed=4)
        assert 150 < pred.expected["points_total"] < 300
        assert pred.expected["margin"] > 0  # l'équipe à domicile est la plus forte
        assert pred.outcome_probs["home"] > 0.5

    def test_thin_sample_stays_realistic(self):
        """Un seul match (ex. présaison) ne doit pas produire un score aberrant."""
        bundle = make_bundle("basket", ([90], [102]), ([102], [90]), league_avg=113.0)
        pred = engine.simulate_basket(bundle, n_sims=10_000, seed=4)
        assert 180 < pred.expected["points_total"] < 250

    def test_spread_and_total_lines_exist(self, bundle):
        pred = engine.simulate_basket(bundle, n_sims=10_000, seed=4)
        assert any(l.key.startswith("total_over_") for l in pred.lines)
        assert any(l.key.startswith("spread_home_") for l in pred.lines)


class TestTennis:
    def test_game_win_prob_properties(self):
        assert math.isclose(engine.game_win_prob(0.5), 0.5, abs_tol=1e-9)
        assert engine.game_win_prob(0.65) > 0.80
        assert engine.game_win_prob(0.35) < 0.20
        values = [engine.game_win_prob(p) for p in np.linspace(0.3, 0.8, 20)]
        assert all(b > a for a, b in zip(values, values[1:]))

    def test_tiebreak_and_set_symmetry(self):
        assert math.isclose(engine.tiebreak_win_prob(0.64, 0.64), 0.5, abs_tol=1e-6)
        hold = engine.game_win_prob(0.64)
        assert math.isclose(engine.set_win_prob(hold, hold, 0.5), 0.5, abs_tol=1e-6)

    def test_tiebreak_terminates_on_extreme_inputs(self):
        """L'égalité prolongée est résolue par formule fermée, sans récursion infinie."""
        for pa, pb in ((0.9, 0.1), (0.5, 0.5), (0.35, 0.92)):
            value = engine.tiebreak_win_prob(pa, pb)
            assert 0.0 <= value <= 1.0

    def test_match_prob_amplifies_set_prob(self):
        assert engine.match_win_prob_from_set(0.6, 3) > 0.6
        assert engine.match_win_prob_from_set(0.6, 5) > engine.match_win_prob_from_set(0.6, 3)
        assert math.isclose(engine.match_win_prob_from_set(0.5, 3), 0.5, abs_tol=1e-9)

    def test_simulation_produces_valid_sets(self):
        bundle = make_bundle(
            "tennis",
            ([1, 1, 1, 0, 1, 1, 0, 1], [0, 0, 0, 1, 0, 0, 1, 0]),
            ([1, 0, 0, 1, 0, 0, 1, 0], [0, 1, 1, 0, 1, 1, 0, 1]),
            league_avg=0.5,
        )
        pred = engine.simulate_tennis(bundle, n_sims=20_000, seed=6)
        assert math.isclose(sum(pred.outcome_probs.values()), 1.0, abs_tol=1e-9)
        assert set(pred.outcome_probs) == {"home", "away"}
        assert all(score in {"2-0", "2-1", "0-2", "1-2"} for score, _p in pred.top_scores)
        assert math.isclose(sum(p for _s, p in pred.top_scores), 1.0, abs_tol=0.01)
        assert 12 < pred.expected["games_total"] < 40

    def test_market_anchoring(self):
        bundle = make_bundle(
            "tennis", ([1, 0, 1, 0], [0, 1, 0, 1]), ([1, 0, 1, 0], [0, 1, 0, 1]),
            odds=make_odds({"Équipe A": 1.25, "Équipe B": 4.00}, books=6),
            league_avg=0.5,
        )
        pred = engine.simulate_tennis(bundle, n_sims=20_000, seed=6)
        # 60 % marché / 40 % modèle (modèle ≈ 50 %) → nettement au-dessus de 0.5
        assert 0.60 < pred.outcome_probs["home"] < 0.85
        assert pred.expected["hold_home"] > pred.expected["hold_away"]


# ==========================================================================
# 6. Valeur, confiance, verdict, assemblage
# ==========================================================================
class TestValueAndConfidence:
    def _bundle_with_odds(self, h2h, books=8):
        strong = ([3, 4, 2, 3, 3, 2, 4, 3, 2, 3], [0, 1, 0, 1, 0, 1, 0, 0, 1, 0])
        weak = ([0, 1, 0, 1, 0, 0, 1, 0, 1, 0], [3, 2, 3, 2, 4, 3, 2, 3, 2, 3])
        return make_bundle("football", strong, weak, odds=make_odds(h2h, books))

    def test_value_requires_threshold(self):
        line = engine.MarketLine("t", "Test", prob=0.55)
        engine.attach_market_comparison(line, 2.00, 0.52)
        assert not line.is_value  # écart de 3 points < seuil 5 %
        line2 = engine.MarketLine("t", "Test", prob=0.60)
        engine.attach_market_comparison(line2, 2.00, 0.50)
        assert line2.is_value and math.isclose(line2.edge, 0.10, abs_tol=1e-9)
        assert math.isclose(line2.expected_value, 0.20, abs_tol=1e-9)

    def test_reported_value_bets_respect_threshold(self):
        pred = engine.analyse(
            self._bundle_with_odds({"Équipe A": 1.40, "Draw": 4.60, "Équipe B": 8.00}),
            n_sims=20_000, seed=2,
        )
        for line in pred.value_bets:
            assert line.edge >= cfg.ENGINE.value_threshold

    def test_confidence_drops_without_odds(self):
        with_odds = engine.analyse(
            self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50}),
            n_sims=10_000, seed=2,
        )
        strong = ([3, 4, 2, 3, 3, 2, 4, 3, 2, 3], [0, 1, 0, 1, 0, 1, 0, 0, 1, 0])
        weak = ([0, 1, 0, 1, 0, 0, 1, 0, 1, 0], [3, 2, 3, 2, 4, 3, 2, 3, 2, 3])
        without = engine.analyse(make_bundle("football", strong, weak), n_sims=10_000, seed=2)
        assert with_odds.confidence.score > without.confidence.score
        assert 0 <= without.confidence.score <= 10
        assert "Cotes indisponibles" in without.badges
        assert any("Cotes" in r for r in without.confidence.reasons)

    def test_confidence_rewards_richer_data(self):
        base = self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50})
        rich = self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50})
        rich.standings = {
            "Équipe A": Standing("Équipe A", 1, 20, 45, 42, 15),
            "Équipe B": Standing("Équipe B", 18, 20, 15, 14, 40),
        }
        now = datetime.now(UTC)
        rich.h2h = [MatchResult(now - timedelta(days=200), "Équipe B", True, 2, 1)]
        assert (
            engine.analyse(rich, n_sims=10_000, seed=2).confidence.score
            > engine.analyse(base, n_sims=10_000, seed=2).confidence.score
        )

    def test_confidence_penalises_thin_history(self):
        pred = engine.analyse(
            make_bundle("football", ([2, 1], [1, 0]), ([1, 0], [2, 1])), n_sims=10_000, seed=2
        )
        assert pred.confidence.score < 6
        assert any("mince" in r for r in pred.confidence.reasons)

    def test_confidence_penalises_bookmaker_disagreement(self):
        agreeing = self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50})
        agreeing.odds.per_book_h2h = {
            "A": {"Équipe A": 1.60}, "B": {"Équipe A": 1.61}, "C": {"Équipe A": 1.59},
        }
        split = self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50})
        split.odds.per_book_h2h = {
            "A": {"Équipe A": 1.40}, "B": {"Équipe A": 1.85}, "C": {"Équipe A": 1.60},
        }
        low = engine.analyse(split, n_sims=10_000, seed=2)
        high = engine.analyse(agreeing, n_sims=10_000, seed=2)
        assert low.confidence.components["marché"] < high.confidence.components["marché"]
        assert any("désaccord" in r for r in low.confidence.reasons)

    def test_verdict_is_short(self):
        pred = engine.analyse(
            self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50}),
            n_sims=10_000, seed=2,
        )
        assert pred.verdict.count(".") <= 4
        assert len(pred.verdict) < 320
        assert pred.risk and "\n" not in pred.risk
        assert len(pred.risk) < 200

    def test_analyse_realigns_home_away_from_market(self):
        """Si le marché place « Équipe B » à domicile, l'ordre doit être corrigé."""
        odds = make_odds(
            {"Équipe B": 2.00, "Draw": 3.40, "Équipe A": 3.80},
            home="Équipe B", away="Équipe A",
        )
        bundle = make_bundle(
            "football",
            ([2, 1, 2, 1, 2, 1, 2, 1], [1, 1, 0, 1, 1, 0, 1, 1]),
            ([1, 2, 1, 2, 1, 2, 1, 2], [1, 0, 1, 1, 0, 1, 1, 0]),
            odds=odds,
        )
        pred = engine.analyse(bundle, n_sims=10_000, seed=2)
        assert pred.venue_swapped is True
        assert pred.home == "Équipe B" and pred.away == "Équipe A"

    def test_analyse_attaches_competition_and_profiles(self):
        pred = engine.analyse(
            self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50}),
            n_sims=10_000, seed=2,
        )
        assert pred.competition is not None and pred.competition.key == "premier_league"
        assert pred.profile_home.matches == 10
        assert pred.profile_away.form_string
        assert pred.bookmaker_count == 8

    def test_main_pick_is_always_present_and_coherent(self):
        pred = engine.analyse(
            self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50}),
            n_sims=20_000, seed=2,
        )
        pick = pred.main_pick
        assert pick is not None
        assert 0.0 < pick.probability <= 1.0
        assert pick.confidence == pred.confidence.score
        assert pick.family and pick.label
        # Le pronostic mis en avant doit correspondre à une ligne réellement calculée.
        assert any(l.key == pick.key for l in pred.lines)

    def test_main_pick_ignores_near_certain_markets(self):
        """« Plus de 1,5 but » à 90 % est vrai mais n'apprend rien : à écarter."""
        pred = engine.analyse(
            self._bundle_with_odds({"Équipe A": 1.40, "Draw": 4.60, "Équipe B": 8.00}),
            n_sims=20_000, seed=2,
        )
        assert pred.main_pick.probability <= cfg.ENGINE.pick_max_probability
        over15 = pred.line("total_over_1.5")
        if over15 and over15.prob > cfg.ENGINE.pick_max_probability:
            assert pred.main_pick.key != "total_over_1.5"

    def test_main_pick_never_recommends_the_draw(self):
        balanced = make_bundle(
            "football",
            ([1, 1, 1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1, 1, 1]),
            ([1, 1, 1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 1, 1, 1]),
        )
        pred = engine.analyse(balanced, n_sims=20_000, seed=3)
        assert pred.main_pick is None or pred.main_pick.key != "1x2_draw"

    def test_main_pick_prefers_a_clear_winner(self):
        """Face à un favori net, le vainqueur prime sur les marchés annexes."""
        pred = engine.analyse(
            self._bundle_with_odds({"Équipe A": 1.55, "Draw": 4.20, "Équipe B": 6.50}),
            n_sims=20_000, seed=7,
        )
        winner = pred.line("1x2_home")
        if cfg.ENGINE.pick_min_probability <= winner.prob <= cfg.ENGINE.pick_max_probability:
            assert pred.main_pick.key == "1x2_home"

    def test_main_pick_strength_labels(self):
        pick = engine.MainPick("k", "Test", "Vainqueur", 0.75, 7.0)
        assert pick.strength == "Fort"
        assert engine.MainPick("k", "T", "V", 0.58, 7.0).strength == "Solide"
        assert engine.MainPick("k", "T", "V", 0.47, 7.0).strength == "Prudent"
        assert engine.MainPick("k", "T", "V", 0.30, 7.0).strength == "Incertain"
        assert pick.fair_odds == pytest.approx(1.33, abs=0.01)

    def test_confidence_reacts_to_research_quality(self):
        """Un dossier bien recoupé renforce la confiance, un dossier bancal la réduit."""
        import research as rs

        bundle = self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50})
        good = rs.ResearchReport(competition=comp_for("football"))
        good.used = ["the_odds_api", "api_football", "openfootball"]
        good.fields_found = ["cotes", "forme", "classement"]
        poor = rs.ResearchReport(competition=comp_for("football"))
        poor.used = ["thesportsdb"]
        poor.fields_found = ["forme"]
        poor.fields_missing = ["cotes", "classement", "confrontations"]

        high = engine.analyse(bundle, n_sims=10_000, seed=2, report=good).confidence.score
        low = engine.analyse(bundle, n_sims=10_000, seed=2, report=poor).confidence.score
        neutral = engine.analyse(bundle, n_sims=10_000, seed=2).confidence.score
        assert low < neutral < high
        assert 0 <= low and high <= 10

    def test_record_is_serialisable(self):
        record = engine.analyse(
            self._bundle_with_odds({"Équipe A": 1.60, "Draw": 4.00, "Équipe B": 5.50}),
            n_sims=10_000, seed=2,
        ).to_record()
        import json

        decoded = json.loads(json.dumps(record))
        assert decoded["sport"] == "football"
        assert decoded["competition"] == "Premier League"


class TestScoreCoherence:
    """Le score affiche doit pouvoir se lire a cote du pronostic sans le contredire."""

    @staticmethod
    def _tirages():
        import numpy as np
        # 6 victoires domicile reparties (2-0, 2-1, 3-1), 4 nuls tous en 1-1.
        # Le 1-1 est donc le score modal absolu alors que la victoire domicile
        # est l'issue majoritaire : exactement le cas signale par l'utilisateur.
        dom = np.array([2, 2, 2, 3, 2, 2, 1, 1, 1, 1])
        ext = np.array([0, 1, 0, 1, 1, 0, 1, 1, 1, 1])
        return dom, ext

    def test_the_absolute_modal_score_can_belong_to_another_outcome(self):
        dom, ext = self._tirages()
        assert engine._score_labels(dom, ext)[0][0] == "1-1"
        assert (dom > ext).mean() == pytest.approx(0.6), "la victoire reste majoritaire"

    def test_conditional_score_matches_the_requested_outcome(self):
        dom, ext = self._tirages()
        got = engine.scores_matching(dom, ext, "home")
        assert got and got[0][0] == "2-0"
        for libelle, _p in got:
            a, b = (int(x) for x in libelle.split("-"))
            assert a > b, "tous les scores proposes doivent donner cette issue"

    def test_probabilities_stay_absolute_not_renormalised(self):
        """Renormaliser gonflerait des chiffres compares a d'autres marches."""
        dom, ext = self._tirages()
        got = engine.scores_matching(dom, ext, "home")
        assert sum(p for _s, p in got) == pytest.approx(0.6, abs=1e-9)

    def test_an_impossible_outcome_returns_nothing(self):
        import numpy as np
        dom, ext = np.array([1, 2]), np.array([0, 0])   # aucune defaite
        assert engine.scores_matching(dom, ext, "away") == []


class TestConsistencyValidator:
    """Garde-fou : attraper une regression future avant que l'utilisateur ne la voie."""

    @staticmethod
    def _pred(**kw):
        base = dict(sport="football", home="A", away="B", n_sims=10,
                    outcome_probs={"home": 0.5, "draw": 0.3, "away": 0.2},
                    market_probs=None, blended_target=None)
        base.update(kw)
        return engine.Prediction(**base)

    def test_a_coherent_prediction_reports_nothing(self):
        pred = self._pred()
        pred.lines = [
            engine.MarketLine("dc_1x", "A ou nul", 0.8),
            engine.MarketLine("btts_yes", "oui", 0.55),
            engine.MarketLine("btts_no", "non", 0.45),
        ]
        assert engine.check_consistency(pred) == []

    def test_outcomes_not_summing_to_one_are_caught(self):
        pred = self._pred(outcome_probs={"home": 0.5, "draw": 0.3, "away": 0.5})
        assert any("100 %" in m for m in engine.check_consistency(pred))

    def test_a_double_chance_out_of_step_is_caught(self):
        pred = self._pred()
        pred.lines = [engine.MarketLine("dc_1x", "A ou nul", 0.95)]   # attendu 0.80
        assert any("dc_1x" in m for m in engine.check_consistency(pred))

    def test_complementary_markets_are_checked(self):
        pred = self._pred()
        pred.lines = [
            engine.MarketLine("btts_yes", "oui", 0.6),
            engine.MarketLine("btts_no", "non", 0.6),
        ]
        assert any("BTTS" in m for m in engine.check_consistency(pred))

    def test_a_score_contradicting_the_pick_is_caught(self):
        pred = self._pred()
        pred.main_pick = engine.MainPick(
            key="1x2_home", label="A gagne", family="vainqueur",
            probability=0.5, confidence=7.0,
        )
        pred.pick_scores = [("1-1", 0.12)]        # un nul sous un pronostic de victoire
        assert any("contredit" in m for m in engine.check_consistency(pred))
