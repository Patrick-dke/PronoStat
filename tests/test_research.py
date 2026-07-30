"""Tests du moteur de recherche approfondie : fusion, saisons, fiabilité."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
import data_sources as ds  # noqa: E402
import research  # noqa: E402
from config import Competition  # noqa: E402

UTC = timezone.utc


def comp(sport: str, key: str) -> Competition:
    found = cfg.competition(sport, key)
    assert found is not None
    return found


def prov(source: str, season: int | None = None, age_days: float = 0.0) -> ds.Provenance:
    return ds.Provenance(
        source=source,
        label=cfg.public_name(source),
        fetched_at=datetime.now(UTC) - timedelta(days=age_days),
        detail="test",
        season=season,
    )


class _Roster(ds.BaseProvider):
    """Source factice renvoyant un effectif fixe."""

    def __init__(self, name, names, season=None, age_days=0.0):
        self.name = name
        self.label = name
        self._names = names
        self._season = season
        self._age = age_days
        self.enabled = True
        self.supports = frozenset({"football", "basket", "hockey", "tennis"})
        self.provides_odds = False

    def handles(self, competition):
        return True

    def participants(self, competition):
        return list(self._names), prov(self.name, self._season, self._age)


class _Hub:
    """Faux agrégateur : uniquement ce dont DeepResearch a besoin."""

    def __init__(self, providers):
        self.providers = providers

    def sources_for(self, competition):
        return list(self.providers)

    def league_context(self, competition, bundle):
        return {"avg_per_team": 1.4, "estimated": False, "n": 0}


# ==========================================================================
# Exécution parallèle
# ==========================================================================
class TestGather:
    def test_runs_everything_and_survives_failures(self):
        def boom():
            raise RuntimeError("source HS")

        out = research.gather({"ok": lambda: 42, "ko": boom, "vide": lambda: None})
        assert out == {"ok": 42, "ko": None, "vide": None}

    def test_empty_input(self):
        assert research.gather({}) == {}

    def test_calls_really_run_in_parallel(self):
        """Six appels d'un dixième de seconde : 0,6 s en série, ~0,1 s en parallèle.

        Le seuil garde une marge confortable pour l'amorçage des threads, tout
        en restant très en dessous du temps d'une exécution séquentielle.
        """
        tasks = {f"t{i}": (lambda: (time.sleep(0.1), i)[1]) for i in range(6)}
        started = time.time()
        research.gather(tasks, max_workers=6)
        elapsed = time.time() - started
        assert elapsed < 0.35, f"{elapsed:.2f}s : les appels semblent séquentiels"


# ==========================================================================
# Fusion des effectifs
# ==========================================================================
class TestRosterMerge:
    def _run(self, providers, competition=None):
        competition = competition or comp("football", "premier_league")
        engine = research.DeepResearch(_Hub(providers))
        return engine.roster(competition)

    def test_reference_source_alone_is_enough(self):
        result = self._run([_Roster("wikipedia", ["Arsenal", "Chelsea", "Everton"], 2026)])
        assert result.names == ["Arsenal", "Chelsea", "Everton"]
        assert result.season == 2026

    def test_low_reliability_source_needs_confirmation(self):
        """Une base peu fiable ne peut pas introduire seule une équipe."""
        result = self._run([
            _Roster("wikipedia", ["Arsenal", "Chelsea"], 2026),
            _Roster("thesportsdb", ["Arsenal", "Équipe Fantôme"]),
        ])
        assert "Arsenal" in result.names
        assert "Équipe Fantôme" not in result.names
        assert "Équipe Fantôme" in result.unconfirmed

    def test_seasons_are_never_mixed(self):
        """À l'intersaison, la source restée sur l'ancienne saison est écartée."""
        result = self._run([
            _Roster("openfootball", ["Burnley", "Wolves", "West Ham"], 2025),
            _Roster("wikipedia", ["Coventry City", "Hull City", "Ipswich Town"], 2026),
        ])
        assert result.season == 2026
        assert result.names == ["Coventry City", "Hull City", "Ipswich Town"]
        assert "Burnley" not in result.names

    def test_undated_source_confirms_but_never_introduces(self):
        result = self._run([
            _Roster("wikipedia", ["Arsenal", "Chelsea"], 2026),
            _Roster("wikidata", ["Arsenal", "Chelsea", "Ancien Club"]),
        ])
        assert set(result.names) == {"Arsenal", "Chelsea"}
        assert "Ancien Club" in result.unconfirmed

    def test_duplicates_across_sources_are_merged(self):
        result = self._run([
            _Roster("wikipedia", ["Arsenal", "Manchester United"], 2026),
            _Roster("openfootball", ["Arsenal FC", "Manchester United FC"], 2026),
        ])
        assert len(result.names) == 2

    def test_best_source_provides_the_display_name(self):
        result = self._run([
            _Roster("openfootball", ["Arsenal FC"], 2026),      # fiabilité 0.85
            _Roster("thesportsdb", ["Arsenal"], 2026),          # fiabilité 0.55
        ])
        assert result.names == ["Arsenal FC"]

    def test_freshness_breaks_ties(self):
        """À fiabilité égale, la donnée la plus récente l'emporte."""
        result = self._run([
            _Roster("wikipedia", ["Vieux Nom"], 2026, age_days=30),
            _Roster("openfootball", ["Nom Récent"], 2026, age_days=0),
        ])
        assert "Nom Récent" in result.names

    def test_last_resort_completion_when_roster_too_short(self):
        """Plutôt qu'une liste amputée, on accepte les noms isolés."""
        squad = [f"Club {i}" for i in range(18)]
        result = self._run([
            _Roster("wikipedia", squad, 2026),
            _Roster("thesportsdb", ["Club 90", "Club 91"], 2026),
        ])
        assert len(result.names) == 20  # effectif attendu de la Premier League

    def test_no_source_returns_empty_result(self):
        result = self._run([])
        assert result.names == [] and result.reliability == 0.0

    def test_coverage_and_reliability(self):
        squad = [f"Club {i}" for i in range(20)]
        full = self._run([_Roster("wikipedia", squad, 2026)])
        assert full.coverage == 1.0 and full.is_complete
        assert 0.5 < full.reliability <= 1.0

        partial = self._run([_Roster("wikipedia", squad[:10], 2026)])
        assert partial.coverage == 0.5 and not partial.is_complete
        assert partial.reliability < full.reliability

    def test_unknown_expected_size_leaves_coverage_undefined(self):
        result = self._run(
            [_Roster("wikipedia", ["A", "B", "C", "D"], 2026)],
            competition=comp("tennis", "atp_tour"),
        )
        assert result.coverage is None


# ==========================================================================
# Extraction Wikipédia
# ==========================================================================
class TestWikipediaExtraction:
    def test_wikilinks_are_cleaned(self):
        clean = ds.WikipediaProvider._clean
        assert clean("[[Arsenal F.C.|Arsenal]]") == "Arsenal"
        assert clean("[[Liverpool F.C.]]") == "Liverpool F.C."
        assert clean("  Chelsea <ref>x</ref>") == "Chelsea"

    def test_only_the_league_table_is_read(self):
        """Une page contenant deux tableaux ne doit pas mélanger les divisions."""
        content = (
            "{{#invoke:Sports table|main|style=WDL"
            "|team1=ARS |name_ARS=[[Arsenal]]"
            "|team2=CHE |name_CHE=[[Chelsea]]"
            "|team3=LIV |name_LIV=[[Liverpool]]"
            "|team4=EVE |name_EVE=[[Everton]]}}"
            "== Promotion ==\n"
            "{{#invoke:Sports table|main|style=WDL"
            "|name_FRO=[[Frosinone]]|name_MON=[[Monza]]"
            "|name_VEN=[[Venezia]]|name_PIS=[[Pisa]]}}"
        )
        first = ds.WikipediaProvider._extract_table(content)
        assert first == ["Arsenal", "Chelsea", "Everton", "Liverpool"]

    def test_expected_size_selects_the_right_table(self):
        content = (
            "{{#invoke:Sports table|main"
            "|name_AAA=[[Alpha]]|name_BBB=[[Beta]]|name_CCC=[[Gamma]]"
            "|name_DDD=[[Delta]]|name_EEE=[[Epsilon]]}}"
            "{{#invoke:Sports table|main"
            "|name_FFF=[[Zeta]]|name_GGG=[[Eta]]|name_HHH=[[Theta]]}}"
        )
        assert ds.WikipediaProvider._extract_table(content, expected=3) == [
            "Eta", "Theta", "Zeta"
        ]
        assert len(ds.WikipediaProvider._extract_table(content, expected=5)) == 5

    def test_falls_back_when_no_table_markup(self):
        content = "|name_AAA=[[Alpha]]\n|name_BBB=[[Beta]]\n|name_CCC=[[Gamma]]\n|name_DDD=[[Delta]]"
        assert ds.WikipediaProvider._extract_table(content) == [
            "Alpha", "Beta", "Delta", "Gamma"
        ]


# ==========================================================================
# Rapport de recherche
# ==========================================================================
class TestResearchReport:
    def _report(self, used, found, missing, issues=()):
        report = research.ResearchReport(competition=comp("football", "premier_league"))
        report.used = list(used)
        report.fields_found = list(found)
        report.fields_missing = list(missing)
        report.inconsistencies = list(issues)
        return report

    def test_reliability_rises_with_sources_and_coverage(self):
        poor = self._report(["thesportsdb"], ["forme"], ["cotes", "classement"])
        rich = self._report(
            ["the_odds_api", "api_football", "openfootball"],
            ["cotes", "forme", "classement", "confrontations"],
            [],
        )
        assert rich.reliability > poor.reliability
        assert rich.label in {"Fiable", "Très fiable"}
        assert poor.label in {"Limitée", "Partielle"}

    def test_inconsistencies_lower_the_score(self):
        clean = self._report(["the_odds_api", "api_football"], ["cotes", "forme"], [])
        noisy = self._report(
            ["the_odds_api", "api_football"], ["cotes", "forme"], [],
            issues=[research.Inconsistency("cotes", "Bookmakers en désaccord",
                                           ("cotes",), severity=1.0)],
        )
        assert noisy.reliability < clean.reliability

    def test_no_source_means_no_reliability(self):
        assert self._report([], [], ["cotes"]).reliability == 0.0

    def test_reliability_stays_within_bounds(self):
        extreme = self._report(
            ["nhl_api", "the_odds_api", "api_football", "football_data", "openfootball"],
            ["cotes", "forme", "classement"], [],
        )
        assert 0.0 <= extreme.reliability <= 1.0

    def test_coverage_counts_found_over_searched(self):
        report = self._report(["x"], ["a", "b"], ["c", "d"])
        assert report.coverage == pytest.approx(0.5)


# ==========================================================================
# Contrôles croisés
# ==========================================================================
class TestCrossChecks:
    def _engine(self):
        return research.DeepResearch(_Hub([]))

    def _bundle(self):
        return ds.Bundle(
            sport="football", home="A", away="B",
            competition=comp("football", "premier_league"),
        )

    def test_stale_data_is_flagged(self):
        bundle = self._bundle()
        bundle.provenances = [prov("thesportsdb", age_days=5)]
        report = research.ResearchReport(competition=bundle.competition)
        self._engine()._cross_check(bundle, report)
        assert any(i.field == "fraîcheur" for i in report.inconsistencies)

    def test_bookmaker_disagreement_is_flagged(self):
        bundle = self._bundle()
        bundle.odds = ds.OddsSnapshot(
            home_team="A", away_team="B", commence_time=None, sport_key="k",
            provenance=prov("the_odds_api"), h2h={"A": 1.6, "B": 2.4},
            per_book_h2h={"x": {"A": 1.30}, "y": {"A": 1.95}},
        )
        report = research.ResearchReport(competition=bundle.competition)
        self._engine()._cross_check(bundle, report)
        assert any(i.field == "cotes" for i in report.inconsistencies)

    def test_clean_bundle_raises_nothing(self):
        bundle = self._bundle()
        bundle.provenances = [prov("the_odds_api")]
        report = research.ResearchReport(competition=bundle.competition)
        self._engine()._cross_check(bundle, report)
        assert report.inconsistencies == []

    def test_duplicate_matches_are_removed(self):
        now = datetime.now(UTC)
        twice = [
            ds.MatchResult(now, "Chelsea", True, 2, 1),
            ds.MatchResult(now, "Chelsea FC", True, 2, 1),      # même match, autre graphie
            ds.MatchResult(now - timedelta(days=400), "Chelsea", False, 0, 3),
        ]
        merged = research.DeepResearch._dedupe_matches(twice)
        assert len(merged) == 2

    def test_backfill_completes_missing_statistics(self):
        now = datetime.now(UTC)
        poor = ds.TeamForm(
            "A", "football",
            [ds.MatchResult(now, "Chelsea", True, 2, 1)],
            prov("football_data"),
        )
        rich = ds.TeamForm(
            "A", "football",
            [ds.MatchResult(now, "Chelsea FC", True, 2, 1,
                            extra={"xg_for": 1.7, "corners_for": 6.0})],
            prov("api_football"),
        )
        research.DeepResearch._backfill_stats(poor, rich)
        assert poor.matches[0].extra["xg_for"] == 1.7
        assert poor.extra["backfilled_stats"] == 2

    def test_backfill_never_overwrites_existing_values(self):
        now = datetime.now(UTC)
        target = ds.TeamForm(
            "A", "football",
            [ds.MatchResult(now, "Chelsea", True, 2, 1, extra={"xg_for": 1.0})],
            prov("api_football"),
        )
        other = ds.TeamForm(
            "A", "football",
            [ds.MatchResult(now, "Chelsea", True, 2, 1, extra={"xg_for": 9.9})],
            prov("thesportsdb"),
        )
        research.DeepResearch._backfill_stats(target, other)
        assert target.matches[0].extra["xg_for"] == 1.0
