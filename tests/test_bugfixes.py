"""Tests de non-régression sur les bugs corrigés.

Chaque classe correspond à un défaut réellement observé en production, avec le
cas précis qui l'avait révélé.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
import data_sources as ds  # noqa: E402
import engine  # noqa: E402
from agent.market import MarketAnalyst  # noqa: E402
from agent.validation import DataValidator  # noqa: E402

UTC = timezone.utc


def comp():
    return cfg.competition("football", "premier_league")


# ==========================================================================
# Bug : « Hull City » analysé avec les résultats de « Manchester City »
# ==========================================================================
class TestTeamIdentityConfusion:
    """Un mot générique partagé ne fait pas deux fois la même équipe."""

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Coventry City", "Manchester City"),
            ("Hull City", "Manchester City"),
            ("Leeds United", "Leeds City"),
            ("Manchester United", "Manchester City"),
            ("Sheffield United", "Sheffield Wednesday"),
            ("Real Madrid", "Atletico Madrid"),
            ("Nottingham Forest", "Sherwood Forest"),
            ("Ipswich Town", "Luton Town"),
        ],
    )
    def test_distinct_clubs_never_match(self, a, b):
        assert ds.name_similarity(a, b) < 0.75
        assert ds.best_match(a, [b]) is None

    @pytest.mark.parametrize(
        "a,b",
        [
            ("Arsenal", "Arsenal FC"),
            ("Real Madrid CF", "Real Madrid"),
            ("FC Barcelona", "Barcelona"),
            ("Brighton and Hove Albion", "Brighton & Hove Albion FC"),
            ("Bayern Munchen", "Bayern Munich"),      # variante orthographique
            ("Paris Saint-Germain", "Paris Saint Germain"),
            ("Wolverhampton Wanderers", "Wolverhampton Wanderers FC"),
            ("West Ham United", "West Ham United FC"),
        ],
    )
    def test_same_club_still_matches(self, a, b):
        assert ds.name_similarity(a, b) >= 0.75

    def test_similarity_is_symmetric(self):
        for a, b in (("Coventry City", "Manchester City"), ("Arsenal", "Arsenal FC")):
            assert ds.name_similarity(a, b) == pytest.approx(ds.name_similarity(b, a))

    def test_spelling_variants_survive_the_hardening(self):
        """Le garde-fou ne doit pas rejeter « Munchen » face à « Munich »."""
        assert ds.name_similarity("Bayern Munchen", "Bayern Munich") > 0.85


# ==========================================================================
# Bug : plantage sur les scores openfootball au format liste
# ==========================================================================
class TestOpenFootballScoreShapes:
    """Le même fichier mêle `{"ft": [1,2]}` et `[1,2]`."""

    def test_dict_shape(self):
        match = {"score": {"ft": [4, 2], "ht": [1, 0]}}
        assert ds.OpenFootballProvider._full_time_score(match) == (4.0, 2.0)

    def test_list_shape(self):
        assert ds.OpenFootballProvider._full_time_score({"score": [0, 0]}) == (0.0, 0.0)

    def test_unplayed_match_is_not_a_nil_nil(self):
        """Un match sans score ne doit jamais compter comme un 0-0."""
        for match in ({}, {"score": None}, {"score": {}}, {"score": []},
                      {"score": {"ht": [1, 0]}}):
            assert ds.OpenFootballProvider._full_time_score(match) is None

    def test_extraction_survives_mixed_shapes(self):
        payload = {
            "matches": [
                {"date": "2025-08-15", "team1": "Arsenal FC", "team2": "Chelsea FC",
                 "score": {"ft": [2, 1]}},
                {"date": "2025-08-22", "team1": "Everton FC", "team2": "Arsenal FC",
                 "score": [0, 3]},
                {"date": "2026-09-01", "team1": "Arsenal FC", "team2": "Fulham FC"},
            ]
        }
        got = ds.OpenFootballProvider._extract_team_matches(payload, "Arsenal", "PL")
        assert len(got) == 2                      # le match non joué est ignoré
        assert got[0].scored == 2 and got[0].conceded == 1 and got[0].home
        assert got[1].scored == 3 and got[1].conceded == 0 and not got[1].home

    def test_head_to_head_handles_both_shapes(self):
        payload = {
            "matches": [
                {"date": "2025-08-15", "team1": "Arsenal FC", "team2": "Chelsea FC",
                 "score": [2, 1]},
                {"date": "2026-01-10", "team1": "Chelsea FC", "team2": "Arsenal FC",
                 "score": {"ft": [0, 1]}},
                {"date": "2025-09-01", "team1": "Arsenal FC", "team2": "Fulham FC",
                 "score": [1, 1]},
            ]
        }
        got = ds.OpenFootballProvider._extract_h2h(payload, "Arsenal", "Chelsea", "PL")
        assert len(got) == 2                      # Fulham n'est pas concerné


# ==========================================================================
# Bug : score exact toujours 1-1
# ==========================================================================
def _form(team, scored, conceded):
    now = datetime.now(UTC)
    return ds.TeamForm(
        team, "football",
        [
            ds.MatchResult(now - timedelta(days=4 + 7 * i), f"Adv {i}", i % 2 == 0, s, c,
                           competition="Premier League")
            for i, (s, c) in enumerate(zip(scored, conceded))
        ],
        ds.Provenance("openfootball", "T", now),
    )


def _bundle(home_scored, home_conceded, away_scored, away_conceded, league_avg=1.4):
    bundle = ds.Bundle(sport="football", home="Alpha", away="Beta", competition=comp())
    bundle.form_home = _form("Alpha", home_scored, home_conceded)
    bundle.form_away = _form("Beta", away_scored, away_conceded)
    bundle.league_context = {"avg_per_team": league_avg, "estimated": True, "n": 380}
    bundle.provenances = [ds.Provenance("openfootball", "T", datetime.now(UTC))]
    return bundle


class TestExactScoreVaries:
    """Le score exact doit découler des statistiques, pas d'un comportement
    par défaut."""

    def test_dominant_attack_shifts_the_scoreline(self):
        weak = _bundle([1] * 10, [1] * 10, [1] * 10, [1] * 10)
        strong = _bundle([3] * 10, [0] * 10, [0] * 10, [3] * 10)
        s_weak = engine.analyse(weak, n_sims=20_000, seed=1).top_scores[0][0]
        s_strong = engine.analyse(strong, n_sims=20_000, seed=1).top_scores[0][0]
        assert s_weak != s_strong
        home, away = s_strong.split("-")
        assert int(home) > int(away)

    def test_different_matchups_give_different_scores(self):
        profiles = [
            ([3, 2, 3, 4, 2, 3, 3, 2, 4, 3], [0, 1, 0, 1, 1, 0, 1, 0, 0, 1],
             [0, 1, 0, 1, 0, 0, 1, 0, 1, 0], [3, 2, 3, 2, 4, 3, 2, 3, 2, 3]),
            ([1, 0, 1, 0, 1, 1, 0, 1, 0, 1], [1, 1, 0, 1, 1, 0, 1, 1, 0, 1],
             [1, 1, 1, 0, 1, 1, 1, 0, 1, 1], [1, 0, 1, 1, 0, 1, 0, 1, 1, 0]),
            ([0, 1, 0, 0, 1, 0, 1, 0, 0, 1], [2, 3, 2, 3, 2, 2, 3, 2, 3, 2],
             [3, 2, 4, 3, 2, 3, 3, 4, 2, 3], [0, 1, 0, 1, 0, 1, 0, 0, 1, 0]),
        ]
        scores = {
            engine.analyse(_bundle(*p), n_sims=20_000, seed=1).top_scores[0][0]
            for p in profiles
        }
        assert len(scores) >= 2, f"scores identiques : {scores}"

    def test_lambdas_track_the_data(self):
        strong = _bundle([3] * 10, [0] * 10, [0] * 10, [3] * 10)
        pred = engine.analyse(strong, n_sims=10_000, seed=1)
        assert pred.expected["lambda_home"] > pred.expected["lambda_away"] * 1.5

    def test_high_scoring_league_lifts_the_totals(self):
        low = engine.analyse(
            _bundle([1] * 10, [1] * 10, [1] * 10, [1] * 10, league_avg=1.0),
            n_sims=20_000, seed=1,
        )
        high = engine.analyse(
            _bundle([3] * 10, [3] * 10, [3] * 10, [3] * 10, league_avg=3.0),
            n_sims=20_000, seed=1,
        )
        assert high.expected["goals_total"] > low.expected["goals_total"] * 1.5


# ==========================================================================
# Bug : niveau de confiance identique d'un match à l'autre
# ==========================================================================
class TestConfidenceVaries:
    def test_data_richness_changes_the_score(self):
        thin = _bundle([1], [1], [1], [1])
        rich = _bundle([2] * 10, [1] * 10, [1] * 10, [2] * 10)
        rich.standings = {
            "Alpha": ds.Standing("Alpha", 2, 30, 65, 60, 25),
            "Beta": ds.Standing("Beta", 15, 30, 35, 33, 44),
        }
        rich.odds = ds.OddsSnapshot(
            home_team="Alpha", away_team="Beta", commence_time=None, sport_key="k",
            provenance=ds.Provenance("the_odds_api", "O", datetime.now(UTC)),
            h2h={"Alpha": 1.85, "Draw": 3.55, "Beta": 4.10}, bookmaker_count=9,
        )
        c_thin = engine.analyse(thin, n_sims=10_000, seed=1).confidence.score
        c_rich = engine.analyse(rich, n_sims=10_000, seed=1).confidence.score
        assert c_rich > c_thin + 2.0

    def test_stale_data_lowers_confidence(self):
        fresh = _bundle([2] * 8, [1] * 8, [1] * 8, [2] * 8)
        stale = _bundle([2] * 8, [1] * 8, [1] * 8, [2] * 8)
        stale.provenances = [
            ds.Provenance("openfootball", "T", datetime.now(UTC) - timedelta(days=6))
        ]
        assert (engine.analyse(stale, n_sims=10_000, seed=1).confidence.score
                < engine.analyse(fresh, n_sims=10_000, seed=1).confidence.score)

    def test_several_matchups_do_not_all_score_the_same(self):
        setups = [
            _bundle([2] * 10, [1] * 10, [1] * 10, [2] * 10),
            _bundle([1] * 3, [1] * 3, [1] * 3, [1] * 3),
            _bundle([2] * 6, [1] * 6, [1] * 6, [2] * 6),
        ]
        setups[0].standings = {
            "Alpha": ds.Standing("Alpha", 1, 30, 70, 65, 20),
            "Beta": ds.Standing("Beta", 18, 30, 25, 22, 60),
        }
        scores = {engine.analyse(b, n_sims=10_000, seed=1).confidence.score
                  for b in setups}
        assert len(scores) >= 2


# ==========================================================================
# Bug : « Cotes indisponibles » sans explication
# ==========================================================================
class TestOddsDiagnostics:
    def test_missing_key_is_named(self):
        diag = ds.OddsDiagnostics()
        diag.add("the_odds_api", "no_key")
        assert "clé" in diag.reason.lower()
        assert "ODDS_API_KEY" in diag.actionable_hint
        assert not diag.succeeded

    def test_the_most_advanced_stage_wins(self):
        """Une source muette et une source qui a cherché : la seconde informe."""
        diag = ds.OddsDiagnostics()
        diag.add("a", "no_key")
        diag.add("b", "event_not_found", "aucun match correspondant")
        assert diag.reason == "aucun match correspondant"

    def test_success_is_reported_as_such(self):
        diag = ds.OddsDiagnostics()
        diag.add("a", "event_not_found")
        diag.add("b", "success", "6 bookmakers", success=True)
        assert diag.succeeded
        assert diag.reason == ds.ODDS_REASONS["success"]
        assert diag.actionable_hint is None

    def test_quota_gives_a_specific_hint(self):
        diag = ds.OddsDiagnostics()
        diag.add("the_odds_api", "quota_exhausted")
        assert "quota" in diag.actionable_hint.lower()

    def test_empty_diagnostics_still_answers(self):
        assert ds.OddsDiagnostics().reason
        assert ds.OddsDiagnostics().actionable_hint

    def test_disabled_source_records_the_reason(self):
        provider = ds.TheOddsApiProvider(
            ds.HttpClient(ds.CacheStore(), ds.QuotaTracker()), api_key=""
        )
        diag = ds.OddsDiagnostics()
        assert provider.odds(comp(), "Arsenal", "Chelsea", diag) is None
        assert [a.stage for a in diag.attempts] == ["no_key"]

    def test_market_read_surfaces_the_reason(self):
        bundle = ds.Bundle(sport="football", home="A", away="B", competition=comp())
        bundle.odds_diagnostics.add("the_odds_api", "event_not_found", "hors calendrier")
        read = MarketAnalyst().read(bundle)
        assert not read.available
        assert read.unavailable_reason == "hors calendrier"
        assert read.unavailable_hint


# ==========================================================================
# Bug : appariement d'événement trop permissif
# ==========================================================================
class TestEventMatching:
    EVENTS = [
        {"id": "1", "home_team": "Manchester City", "away_team": "Everton"},
        {"id": "2", "home_team": "Arsenal", "away_team": "Chelsea"},
    ]

    def test_exact_pair_is_found(self):
        event, score = ds.TheOddsApiProvider._find_event(self.EVENTS, "Arsenal", "Chelsea")
        assert event is not None and event["id"] == "2" and score > 0.95

    def test_reversed_pair_is_found(self):
        event, _ = ds.TheOddsApiProvider._find_event(self.EVENTS, "Chelsea", "Arsenal")
        assert event is not None and event["id"] == "2"

    def test_lookalike_pair_is_rejected(self):
        """« Coventry City vs Everton » ne doit pas trouver Manchester City."""
        event, score = ds.TheOddsApiProvider._find_event(
            self.EVENTS, "Coventry City", "Everton"
        )
        assert event is None
        assert 0.0 <= score <= 1.0      # le score est renvoyé pour le diagnostic

    def test_absent_match_reports_its_best_score(self):
        event, score = ds.TheOddsApiProvider._find_event(self.EVENTS, "Lyon", "Monaco")
        assert event is None and score < 0.75

    def test_empty_calendar(self):
        assert ds.TheOddsApiProvider._find_event([], "Arsenal", "Chelsea") == (None, 0.0)


# ==========================================================================
# Validation : appartenance à la compétition, doublons
# ==========================================================================
class TestValidationHardening:
    def test_history_from_another_competition_is_flagged(self):
        bundle = _bundle([2] * 6, [1] * 6, [1] * 6, [2] * 6)
        for match in bundle.form_away.matches:
            match.competition = "English League Championship"
        report = DataValidator().validate(bundle)
        assert any("Championship" in w for w in report.warnings)

    def test_matching_competition_raises_nothing(self):
        report = DataValidator().validate(_bundle([2] * 6, [1] * 6, [1] * 6, [2] * 6))
        assert not any("pas de" in w for w in report.warnings)

    def test_duplicate_matches_are_removed(self):
        bundle = _bundle([2] * 5, [1] * 5, [1] * 5, [2] * 5)
        bundle.form_home.matches.append(bundle.form_home.matches[0])
        DataValidator().validate(bundle)
        assert bundle.form_home.n == 5

    def test_warnings_are_deduplicated(self):
        """Deux équipes produisent le même message : il ne doit pas masquer
        les autres."""
        report = DataValidator().validate(_bundle([1], [1], [1], [1]))
        assert len(report.warnings) == len(set(report.warnings))


class TestSecretsReport:
    """Le rapport de configuration s'affiche dans l'interface : aucune valeur
    de clé ne doit pouvoir en sortir, sous aucune forme."""

    def test_no_key_value_ever_leaks(self, monkeypatch):
        import json
        secret = "SUPERSECRET1234567890abcdef"
        monkeypatch.setenv("ODDS_API_KEY", secret)
        rapport = cfg.secrets_report()
        serialise = json.dumps(rapport, default=str)
        assert secret not in serialise
        assert secret[:8] not in serialise, "même un fragment ne doit pas apparaître"
        assert str(len(secret)) in rapport["origine"]["ODDS_API_KEY"]

    def test_absent_key_is_named_as_such(self, monkeypatch):
        monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
        rapport = cfg.secrets_report()
        assert rapport["origine"]["RAPIDAPI_KEY"] == "ABSENTE"
