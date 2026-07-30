"""Tests de la source d'archives (statistiques détaillées + cotes de clôture).

Point de vigilance central : ces cotes concernent des matchs **déjà joués**.
Elles ne doivent jamais être présentées comme la cote d'une rencontre à venir.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
import data_sources as ds  # noqa: E402
import engine  # noqa: E402
from agent.market import MarketAnalyst  # noqa: E402

UTC = timezone.utc

HEADER = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST,"
          "HC,AC,HY,AY,HR,AR,AvgH,AvgD,AvgA")

# Les seuils du fournisseur exigent un volume de vraie saison (au moins trois
# matchs par équipe et par lieu). On construit donc un mini-championnat
# complet plutôt qu'une poignée de lignes.
_TEAMS = ["Arsenal", "Man City", "Chelsea", "Wolves", "Everton", "Fulham"]
# Force relative de chaque club : pilote scores et cotes du jeu de test.
_RANK = {name: len(_TEAMS) - i for i, name in enumerate(_TEAMS)}


def _odds_triple(gap: int) -> tuple[float, float, float]:
    """Cotes réalistes : marge d'environ 6 %, comme un vrai livre.

    Des cotes qui somment à moins de 100 % seraient rejetées par le contrôle
    de cohérence — à raison, aucun bookmaker n'en propose.
    """
    p_home = min(0.70, max(0.12, 0.36 + 0.055 * gap))
    p_draw = 0.26
    p_away = max(0.06, 1.0 - p_home - p_draw)
    total = p_home + p_draw + p_away
    overround = 1.06
    return tuple(  # type: ignore[return-value]
        round(total / (p * overround), 2) for p in (p_home, p_draw, p_away)
    )


def _build_csv() -> str:
    lines = [HEADER]
    day = 1
    for first_leg in (True, False):
        for i, home in enumerate(_TEAMS):
            for j, away in enumerate(_TEAMS):
                if i == j or (first_leg and i > j) or (not first_leg and i < j):
                    continue
                gap = _RANK[home] - _RANK[away]
                hg = max(0, 1 + (gap > 0) + (gap > 2))
                ag = max(0, 1 - (gap > 0) + (gap < -2))
                odds_home, odds_draw, odds_away = _odds_triple(gap)
                date = f"{(day % 28) + 1:02d}/{((day // 28) % 12) + 1:02d}/2025"
                day += 1
                lines.append(
                    f"E0,{date},{home},{away},{hg},{ag},"
                    f"{'H' if hg > ag else ('A' if ag > hg else 'D')},"
                    f"{12 + gap},{12 - gap},{5 + gap},{5 - gap},"
                    f"{5 + gap},{5 - gap},1,2,0,0,"
                    f"{odds_home},{odds_draw},{odds_away}"
                )
    return "\n".join(lines) + "\n"


CSV = _build_csv()


def comp():
    return cfg.competition("football", "premier_league")


class _Http:
    """Client HTTP factice servant toujours le même CSV."""

    def __init__(self, text=CSV, fail=False):
        self.text = text
        self.fail = fail
        self.calls = []
        self.last_error = None

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        if self.fail:
            return None
        return self.text, datetime.now(UTC).timestamp(), False


@pytest.fixture
def provider():
    return ds.FootballDataUkProvider(_Http())


# ==========================================================================
# Lecture des archives
# ==========================================================================
class TestParsing:
    def test_season_code_format(self):
        assert ds.FootballDataUkProvider._season_code(2025) == "2526"
        assert ds.FootballDataUkProvider._season_code(2019) == "1920"

    def test_rows_are_read(self, provider):
        rows = ds.FootballDataUkProvider._parse_csv(CSV)
        assert len(rows) == 30          # championnat de 6 équipes, aller-retour
        assert rows[0]["HomeTeam"] in _TEAMS

    def test_byte_order_mark_is_tolerated(self):
        rows = ds.FootballDataUkProvider._parse_csv("﻿" + CSV)
        assert len(rows) == 30
        assert "Div" in rows[0] or "﻿Div" in rows[0]

    def test_malformed_input_returns_nothing(self):
        assert ds.FootballDataUkProvider._parse_csv("") == []

    def test_abbreviations_are_resolved(self, provider):
        assert provider._canonical("Man City") == "Manchester City"
        assert provider._canonical("Nott'm Forest") == "Nottingham Forest"
        assert provider._canonical("Ath Madrid") == "Atlético Madrid"
        assert provider._canonical("Wolves") == "Wolverhampton Wanderers"

    def test_unknown_name_is_left_untouched(self, provider):
        assert provider._canonical("Arsenal") == "Arsenal"

    def test_abbreviation_matches_the_full_name(self, provider):
        assert provider._same_team("Man City", "Manchester City")
        assert provider._same_team("Wolves", "Wolverhampton Wanderers")
        assert not provider._same_team("Man City", "Coventry City")


# ==========================================================================
# Forme et statistiques détaillées
# ==========================================================================
class TestRichStats:
    def test_form_carries_the_detailed_statistics(self, provider):
        form = provider.form(comp(), "Arsenal")
        assert form is not None and form.n == cfg.FORM_WINDOW
        assert form.extra_avg("corners_for") is not None
        assert form.extra_avg("corners_against") is not None
        assert form.extra_avg("shots") is not None
        assert form.extra_avg("shots_on_target") is not None
        assert form.extra_avg("yellow_cards") is not None

    def test_perspective_is_correct_home_and_away(self, provider):
        """Les statistiques doivent suivre l'équipe, pas la colonne du fichier."""
        form = provider.form(comp(), "Arsenal")
        home_games = [m for m in form.matches if m.home]
        away_games = [m for m in form.matches if not m.home]
        assert home_games and away_games
        assert all(m.opponent != "Arsenal" for m in form.matches)
        # Arsenal est le club le plus fort du jeu de test : il marque plus
        # qu'il n'encaisse, à domicile comme à l'extérieur.
        assert all(m.scored >= m.conceded for m in form.matches)
        # Les cartons jaunes valent 1 pour le domicile, 2 pour l'extérieur.
        assert all(m.extra["yellow_cards"] == 1 for m in home_games)
        assert all(m.extra["yellow_cards"] == 2 for m in away_games)

    def test_corners_total_is_the_sum(self, provider):
        form = provider.form(comp(), "Arsenal")
        for match in form.matches:
            if "corners_for" in match.extra and "corners_against" in match.extra:
                assert match.extra["corners_total"] == pytest.approx(
                    match.extra["corners_for"] + match.extra["corners_against"]
                )

    def test_matches_are_most_recent_first(self, provider):
        dates = [m.date for m in provider.form(comp(), "Arsenal").matches]
        assert dates == sorted(dates, reverse=True)

    def test_unknown_team_yields_nothing(self, provider):
        assert provider.form(comp(), "Real Madrid") is None

    def test_head_to_head_only_returns_that_pairing(self, provider):
        got = provider.head_to_head(comp(), "Manchester City", "Chelsea")
        assert got is not None
        matches, _prov = got
        assert len(matches) == 2          # 05/09 et 19/09
        assert all(m.opponent == "Chelsea" for m in matches)

    def test_source_is_skipped_when_unmapped(self):
        provider = ds.FootballDataUkProvider(_Http())
        ucl = cfg.competition("football", "ucl")      # pas de code d'archive
        assert not provider.handles(ucl)
        assert provider.form(ucl, "Arsenal") is None

    def test_network_failure_is_silent(self):
        provider = ds.FootballDataUkProvider(_Http(fail=True))
        assert provider.form(comp(), "Arsenal") is None
        assert provider.market_ratings(comp()) is None


# ==========================================================================
# Repère de marché — dérivé des cotes de clôture
# ==========================================================================
class TestMarketReference:
    def test_ratings_are_probability_triples(self, provider):
        got = provider.market_ratings(comp())
        assert got is not None
        profiles, prov = got
        for team, entry in profiles.items():
            for side in ("home", "away"):
                if side in entry:
                    assert len(entry[side]) == 3
                    assert math.isclose(sum(entry[side]), 1.0, abs_tol=1e-6)
        assert "cotes de clôture" in prov.detail

    def test_margin_is_removed(self, provider):
        """Les cotes brutes somment à plus de 100 % : la marge doit partir."""
        row = {"AvgH": "1.80", "AvgD": "3.90", "AvgA": "4.20"}
        probs = provider._row_odds(row)
        assert probs is not None
        assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
        assert probs[0] > probs[1] > probs[2]

    def test_incoherent_odds_are_ignored(self, provider):
        assert provider._row_odds({"AvgH": "5.0", "AvgD": "5.0", "AvgA": "5.0"}) is None
        assert provider._row_odds({"AvgH": "", "AvgD": "", "AvgA": ""}) is None
        assert provider._row_odds({}) is None

    def test_bookmaker_columns_are_tried_in_order(self, provider):
        """Sans moyenne du marché, on retombe sur un bookmaker de référence."""
        row = {"AvgH": "", "AvgD": "", "AvgA": "", "B365H": "2.00",
               "B365D": "3.40", "B365A": "3.80"}
        probs = provider._row_odds(row)
        assert probs is not None and math.isclose(sum(probs), 1.0, abs_tol=1e-9)

    def test_stronger_team_gets_a_better_profile(self, provider):
        profiles, _prov = provider.market_ratings(comp())
        # Arsenal est donné favori partout dans le jeu de test.
        assert profiles["Arsenal"]["home"][0] > 0.5


# ==========================================================================
# Le repère n'est jamais confondu avec une cote de match
# ==========================================================================
class TestReferenceIsNotLiveOdds:
    def _bundle(self, with_reference: bool):
        bundle = ds.Bundle(sport="football", home="Alpha", away="Beta",
                           competition=comp())
        now = datetime.now(UTC)
        for slot, scored, conceded in (("form_home", 2, 1), ("form_away", 1, 2)):
            setattr(bundle, slot, ds.TeamForm(
                slot, "football",
                [ds.MatchResult(now - timedelta(days=4 + 7 * i), f"Adv {i}",
                                i % 2 == 0, scored, conceded,
                                competition="Premier League")
                 for i in range(8)],
                ds.Provenance("football_data_uk", "A", now),
            ))
        bundle.league_context = {"avg_per_team": 1.4, "estimated": True, "n": 380}
        bundle.provenances = [ds.Provenance("football_data_uk", "A", now)]
        if with_reference:
            bundle.market_reference = {"home": 0.55, "draw": 0.25, "away": 0.20}
            bundle.market_reference_detail = "cotes de clôture de 380 matchs"
        return bundle

    def test_market_is_still_reported_unavailable(self):
        read = MarketAnalyst().read(self._bundle(with_reference=True))
        assert not read.available          # ce n'est pas une cote du match
        assert read.probabilities == {}    # aucune cote publiée
        assert read.reference is not None  # mais le repère est exposé à part

    def test_reference_anchors_the_model(self):
        without = engine.analyse(self._bundle(False), n_sims=20_000, seed=1)
        with_ref = engine.analyse(self._bundle(True), n_sims=20_000, seed=1)
        assert without.diagnostics["anchor"] == "aucun (statistiques seules)"
        assert "repère" in with_ref.diagnostics["anchor"]
        assert with_ref.outcome_probs["home"] != without.outcome_probs["home"]

    def test_reference_weighs_less_than_real_odds(self):
        """Un repère de saison ne doit pas peser autant qu'une cote du match."""
        bundle = self._bundle(True)
        pred = engine.analyse(bundle, n_sims=10_000, seed=1)
        assert pred.diagnostics["anchor_weight"] < cfg.ENGINE.market_weight

    def test_reference_gives_partial_confidence_credit(self):
        none = engine.analyse(self._bundle(False), n_sims=10_000, seed=1)
        ref = engine.analyse(self._bundle(True), n_sims=10_000, seed=1)
        assert 0 < ref.confidence.components["marché"] < 3.0
        assert none.confidence.components["marché"] == 0.0
        assert ref.confidence.score > none.confidence.score

    def test_value_bets_are_never_claimed_without_real_odds(self):
        """Sans cote publiée, aucune « opportunité » ne peut être annoncée."""
        pred = engine.analyse(self._bundle(True), n_sims=20_000, seed=1)
        assert pred.value_bets == []
        assert all(line.odds is None for line in pred.lines)

    def test_market_probs_stay_empty(self):
        pred = engine.analyse(self._bundle(True), n_sims=10_000, seed=1)
        assert pred.market_probs is None
