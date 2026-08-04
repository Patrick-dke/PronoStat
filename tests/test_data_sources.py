"""Tests de la couche données : registre de compétitions, cache, quotas, repli."""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
import data_sources as ds  # noqa: E402
from config import Competition  # noqa: E402

UTC = timezone.utc


def comp(sport: str, key: str) -> Competition:
    found = cfg.competition(sport, key)
    assert found is not None, f"compétition {key} absente du registre"
    return found


# ==========================================================================
# Registre des compétitions (§2 à §5, §10)
# ==========================================================================
class TestCompetitionRegistry:
    def test_keys_are_unique_across_the_registry(self):
        keys = [c.key for c in cfg.all_competitions(include_disabled=True)]
        assert len(keys) == len(set(keys))

    def test_sport_field_matches_its_bucket(self):
        for sport, comps in cfg.SUPPORTED_COMPETITIONS.items():
            assert all(c.sport == sport for c in comps)

    def test_football_covers_the_big_five_and_european_cups(self):
        keys = {c.key for c in cfg.competitions("football")}
        assert {"premier_league", "la_liga", "bundesliga", "serie_a", "ligue_1"} <= keys
        assert {"ucl", "uel", "uecl"} <= keys

    def test_national_cups_are_off_by_default(self):
        """Les coupes nationales restent désactivées tant qu'on ne les active pas."""
        enabled = {c.key for c in cfg.competitions("football")}
        registered = {c.key for c in cfg.competitions("football", include_disabled=True)}
        assert "fa_cup" in registered
        assert ("fa_cup" in enabled) is cfg._CUPS

    def test_hockey_is_nhl_only(self):
        comps = cfg.competitions("hockey")
        assert [c.key for c in comps] == ["nhl"]
        assert comps[0].nhl is True

    def test_basket_is_us_only(self):
        keys = {c.key for c in cfg.competitions("basket", include_disabled=True)}
        assert keys <= {"nba", "wnba", "nba_summer_league", "nba_g_league"}
        assert "nba" in {c.key for c in cfg.competitions("basket")}

    def test_tennis_covers_major_circuits(self):
        keys = {c.key for c in cfg.competitions("tennis")}
        assert {"atp_grand_slam", "wta_grand_slam", "atp_tour", "wta_tour"} <= keys
        assert {"atp_finals", "wta_finals", "davis_cup"} <= keys

    def test_tennis_exclusion_pattern_blocks_secondary_circuits(self):
        for key in ("tennis_atp_challenger_x", "tennis_itf_men", "tennis_wta_futures"):
            assert re.search(cfg.TENNIS_EXCLUDED_PATTERN, key)
        assert not re.search(cfg.TENNIS_EXCLUDED_PATTERN, "tennis_atp_wimbledon")

    def test_cups_declare_a_team_pool(self):
        for c in cfg.competitions("football", include_disabled=True):
            if c.is_cup and c.sport == "football":
                assert c.team_pool, f"{c.key} devrait déclarer un vivier d'équipes"

    def test_scope_is_unique_per_competition(self):
        scopes = [c.scope for c in cfg.all_competitions(include_disabled=True)]
        assert len(scopes) == len(set(scopes))

    def test_lookup_helpers(self):
        assert cfg.competition("football", "premier_league").label == "Premier League"
        assert cfg.competition("football", "inexistante") is None
        assert cfg.competitions("sport_inconnu") == []


# ==========================================================================
# Résolution des clés The Odds API
# ==========================================================================
class TestOddsKeyResolution:
    @pytest.fixture
    def provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        http = ds.HttpClient(ds.CacheStore(tmp_path / "c.json"), ds.QuotaTracker(tmp_path / "q.json"))
        prov = ds.TheOddsApiProvider(http, api_key="fake")
        catalogue = {
            "soccer_epl": {"title": "EPL", "group": "Soccer", "active": True},
            "soccer_spain_la_liga": {"title": "La Liga - Spain", "group": "Soccer", "active": True},
            "tennis_atp_wimbledon": {"title": "ATP Wimbledon", "group": "Tennis", "active": True},
            "tennis_atp_paris_masters": {"title": "ATP Paris", "group": "Tennis", "active": True},
            "tennis_atp_challenger_x": {"title": "ATP Challenger", "group": "Tennis", "active": True},
            "tennis_itf_men": {"title": "ITF Men", "group": "Tennis", "active": True},
            "tennis_wta_wimbledon": {"title": "WTA Wimbledon", "group": "Tennis", "active": True},
            "tennis_atp_finals": {"title": "ATP Finals", "group": "Tennis", "active": True},
            "icehockey_nhl": {"title": "NHL", "group": "Ice Hockey", "active": True},
        }
        monkeypatch.setattr(prov, "catalogue", lambda: catalogue)
        return prov

    def test_direct_key(self, provider):
        assert provider.resolve_keys(comp("football", "premier_league")) == ["soccer_epl"]

    def test_grand_slam_pattern_selects_only_slams(self, provider):
        keys = provider.resolve_keys(comp("tennis", "atp_grand_slam"))
        assert keys == ["tennis_atp_wimbledon"]

    def test_tour_pattern_excludes_slams_and_finals(self, provider):
        keys = provider.resolve_keys(comp("tennis", "atp_tour"))
        assert "tennis_atp_paris_masters" in keys
        assert "tennis_atp_wimbledon" not in keys
        assert "tennis_atp_finals" not in keys

    def test_secondary_circuits_are_never_returned(self, provider):
        for key in ("atp_tour", "atp_grand_slam", "atp_finals"):
            keys = provider.resolve_keys(comp("tennis", key))
            assert not any("challenger" in k or "itf" in k for k in keys)

    def test_unknown_key_falls_back_to_title(self, provider):
        custom = Competition(
            key="x", label="X", sport="football",
            odds_key="soccer_cle_disparue", odds_title="La Liga - Spain",
        )
        assert provider.resolve_keys(custom) == ["soccer_spain_la_liga"]

    def test_no_match_returns_empty(self, provider):
        custom = Competition(key="x", label="X", sport="football", odds_key="soccer_inconnu")
        assert provider.resolve_keys(custom) == []


# ==========================================================================
# Reprise après coupure réseau
#
# Constaté en conditions réelles : un seul ConnectionError faisait perdre les
# cotes d'un match, et la confiance de l'analyse chutait sans raison visible.
# ==========================================================================
class TestNetworkRetry:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        monkeypatch.setattr(cfg, "HTTP_RETRY_DELAY", 0.0)   # pas d'attente en test
        return ds.HttpClient(ds.CacheStore(tmp_path / "c.json"), ds.QuotaTracker(tmp_path / "q.json"))

    @staticmethod
    def _reponse(payload):
        class R:
            status_code = 200
            headers: dict = {}
            def json(self): return payload
        return R()

    def test_recovers_after_a_transient_failure(self, client, monkeypatch):
        appels = []

        def flaky(*_a, **_k):
            appels.append(1)
            if len(appels) == 1:
                raise requests_exceptions_connection()
            return TestNetworkRetry._reponse({"ok": True})

        monkeypatch.setattr(client.session, "get", flaky)
        got = client.get_json("https://exemple.test/x")
        assert got is not None and got[0] == {"ok": True}
        assert len(appels) == 2, "le second essai doit aboutir"

    def test_gives_up_after_the_configured_number_of_tries(self, client, monkeypatch):
        appels = []

        def toujours_ko(*_a, **_k):
            appels.append(1)
            raise requests_exceptions_connection()

        monkeypatch.setattr(client.session, "get", toujours_ko)
        assert client.get_json("https://exemple.test/y") is None
        assert len(appels) == cfg.HTTP_RETRIES + 1
        assert "network" in (client.last_error or "")


def requests_exceptions_connection() -> Exception:
    import requests
    return requests.ConnectionError("panne simulée")


# ==========================================================================
# Affiches réellement programmées
#
# Une compétition de vingt équipes offre 380 appariements, mais une dizaine
# seulement sont au calendrier. Sans cette liste, l'utilisateur compose des
# matchs qui n'existent pas et n'obtient jamais de cotes.
# ==========================================================================
class TestFixtures:
    @pytest.fixture
    def provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        http = ds.HttpClient(ds.CacheStore(tmp_path / "c.json"), ds.QuotaTracker(tmp_path / "q.json"))
        prov = ds.TheOddsApiProvider(http, api_key="fake")
        monkeypatch.setattr(prov, "resolve_keys", lambda _c: ["soccer_epl"])
        return prov

    def test_fixtures_are_sorted_by_kickoff(self, provider, monkeypatch):
        monkeypatch.setattr(provider, "_events", lambda *_a: ([
            {"home_team": "B", "away_team": "C", "commence_time": "2026-08-22T14:00:00Z"},
            {"home_team": "A", "away_team": "D", "commence_time": "2026-08-21T19:00:00Z"},
        ], time.time(), False))
        got = provider.fixtures(comp("football", "premier_league"))
        assert [(f.home, f.away) for f in got] == [("A", "D"), ("B", "C")]

    def test_undated_fixtures_go_last_without_crashing(self, provider, monkeypatch):
        """Une date absente ne doit pas faire échouer le tri."""
        monkeypatch.setattr(provider, "_events", lambda *_a: ([
            {"home_team": "SansDate", "away_team": "X"},
            {"home_team": "A", "away_team": "D", "commence_time": "2026-08-21T19:00:00Z"},
        ], time.time(), False))
        got = provider.fixtures(comp("football", "premier_league"))
        assert [f.home for f in got] == ["A", "SansDate"]
        assert got[-1].starts_at is None
        assert "date à venir" in got[-1].label

    def test_incomplete_entries_are_dropped(self, provider, monkeypatch):
        monkeypatch.setattr(provider, "_events", lambda *_a: ([
            {"home_team": "A"},                     # adversaire manquant
            {"away_team": "B"},                     # hôte manquant
            {"home_team": "C", "away_team": "D", "commence_time": "2026-08-21T19:00:00Z"},
        ], time.time(), False))
        got = provider.fixtures(comp("football", "premier_league"))
        assert [(f.home, f.away) for f in got] == [("C", "D")]

    def test_no_calendar_returns_empty_list(self, provider, monkeypatch):
        """Liste vide, jamais None : l'appelant itère dessus sans précaution."""
        monkeypatch.setattr(provider, "_events", lambda *_a: None)
        assert provider.fixtures(comp("football", "premier_league")) == []


class TestFixtureMerge:
    """Fusion des calendriers : les sources ne nomment pas les clubs pareil."""

    class _Source:
        def __init__(self, name, matchs):
            self.name, self._matchs = name, matchs

        def fixtures(self, _comp):
            return self._matchs

    @staticmethod
    def _hub(tmp_path, monkeypatch, sources):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        monkeypatch.setattr(cfg, "ODDS_HISTORY_FILE", tmp_path / "o.json")
        hub = ds.DataHub()
        hub.providers = sources
        return hub

    def test_same_match_under_two_names_appears_once(self, tmp_path, monkeypatch):
        quand = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)
        hub = self._hub(tmp_path, monkeypatch, [
            self._Source("cotes", [ds.Fixture("Bayern Munich", "VfB Stuttgart", quand)]),
            self._Source("gratuite", [ds.Fixture("Bayern Munich", "Stuttgart", quand)]),
        ])
        got = hub.fixtures(comp("football", "bundesliga"))
        assert len(got) == 1
        assert got[0].away == "VfB Stuttgart", "le libellé de la source prioritaire est conservé"

    def test_similar_names_on_another_date_stay_distinct(self, tmp_path, monkeypatch):
        """Un match aller et un match retour ne doivent pas fusionner."""
        aller = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)
        retour = datetime(2026, 12, 12, 18, 30, tzinfo=UTC)
        hub = self._hub(tmp_path, monkeypatch, [
            self._Source("a", [ds.Fixture("Bayern Munich", "VfB Stuttgart", aller)]),
            self._Source("b", [ds.Fixture("Bayern Munich", "VfB Stuttgart", retour)]),
        ])
        assert len(hub.fixtures(comp("football", "bundesliga"))) == 2

    def test_a_failing_source_does_not_block_the_others(self, tmp_path, monkeypatch):
        class Cassee:
            name = "cassee"
            def fixtures(self, _comp):
                raise RuntimeError("panne")

        quand = datetime(2026, 8, 28, 18, 30, tzinfo=UTC)
        hub = self._hub(tmp_path, monkeypatch, [
            Cassee(),
            self._Source("saine", [ds.Fixture("Bayern Munich", "VfB Stuttgart", quand)]),
        ])
        assert len(hub.fixtures(comp("football", "bundesliga"))) == 1


class TestNhlFixtures:
    """Le calendrier NHL est gratuit et sans clé : il doit vivre sans cotes."""

    @pytest.fixture
    def provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        http = ds.HttpClient(ds.CacheStore(tmp_path / "c.json"), ds.QuotaTracker(tmp_path / "q.json"))
        prov = ds.NhlApiProvider(http)
        prov.enabled = True
        return prov

    @staticmethod
    def _equipe(ville: str, surnom: str) -> dict:
        return {"placeName": {"default": ville}, "commonName": {"default": surnom}}

    def test_city_and_nickname_are_joined(self, provider, monkeypatch):
        """Le calendrier sépare ville et surnom ; l'effectif attend le nom complet."""
        monkeypatch.setattr(provider.http, "get_json", lambda *_a, **_k: ({"gameWeek": [{"games": [{
            "gameType": 2, "startTimeUTC": "2026-09-29T21:00:00Z",
            "homeTeam": self._equipe("Carolina", "Hurricanes"),
            "awayTeam": self._equipe("Florida", "Panthers"),
        }]}]}, time.time(), False))
        got = provider.fixtures(comp("hockey", "nhl"))
        assert [(f.home, f.away) for f in got] == [("Carolina Hurricanes", "Florida Panthers")]

    def test_preseason_games_are_excluded(self, provider, monkeypatch):
        """Un match de préparation fausserait autant l'analyse que le pronostic."""
        monkeypatch.setattr(provider.http, "get_json", lambda *_a, **_k: ({"gameWeek": [{"games": [
            {"gameType": 1, "startTimeUTC": "2026-09-20T21:00:00Z",
             "homeTeam": self._equipe("Boston", "Bruins"),
             "awayTeam": self._equipe("New York", "Rangers")},
            {"gameType": 2, "startTimeUTC": "2026-09-29T21:00:00Z",
             "homeTeam": self._equipe("Carolina", "Hurricanes"),
             "awayTeam": self._equipe("Florida", "Panthers")},
        ]}]}, time.time(), False))
        got = provider.fixtures(comp("hockey", "nhl"))
        assert [f.home for f in got] == ["Carolina Hurricanes"]


# ==========================================================================
# Cotes buteur
#
# Marché non exclusif : plusieurs joueurs marquent dans un même match, la
# somme des probabilités dépasse donc 100 %. Toute normalisation serait fausse.
# ==========================================================================
class TestGoalScorers:
    @pytest.fixture
    def provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        http = ds.HttpClient(ds.CacheStore(tmp_path / "c.json"), ds.QuotaTracker(tmp_path / "q.json"))
        prov = ds.TheOddsApiProvider(http, api_key="fake")
        monkeypatch.setattr(prov, "resolve_keys", lambda _c: ["soccer_epl"])
        demain = datetime.now(UTC) + timedelta(days=1)
        monkeypatch.setattr(prov, "_events", lambda *_a: ([{
            "id": "e1", "home_team": "A", "away_team": "B",
            "commence_time": demain.isoformat(),
        }], time.time(), False))
        return prov

    @staticmethod
    def _repond(prov, monkeypatch, bookmakers):
        monkeypatch.setattr(
            prov.http, "get_json",
            lambda *_a, **_k: ({"bookmakers": bookmakers}, time.time(), False),
        )

    def test_players_sorted_by_probability(self, provider, monkeypatch):
        self._repond(provider, monkeypatch, [{"title": "Book", "markets": [{
            "key": "player_goal_scorer_anytime",
            "outcomes": [
                {"name": "Yes", "description": "Lointain", "price": 8.0},
                {"name": "Yes", "description": "Favori", "price": 2.0},
            ],
        }]}])
        board = provider.goal_scorers(comp("football", "premier_league"), "A", "B")
        assert [s.player for s in board.scorers] == ["Favori", "Lointain"]
        assert board.scorers[0].probability == pytest.approx(0.5)

    def test_best_price_wins_across_bookmakers(self, provider, monkeypatch):
        """La cote la plus généreuse est retenue : elle minore le moins ses chances."""
        marche = lambda prix: {"key": "player_goal_scorer_anytime", "outcomes": [
            {"name": "Yes", "description": "Joueur", "price": prix}]}
        self._repond(provider, monkeypatch, [
            {"title": "Serré", "markets": [marche(3.0)]},
            {"title": "Généreux", "markets": [marche(4.0)]},
        ])
        board = provider.goal_scorers(comp("football", "premier_league"), "A", "B")
        assert len(board.scorers) == 1
        assert board.scorers[0].price == 4.0
        assert board.scorers[0].bookmaker == "Généreux"

    def test_invalid_entries_are_dropped(self, provider, monkeypatch):
        self._repond(provider, monkeypatch, [{"title": "Book", "markets": [{
            "key": "player_goal_scorer_anytime",
            "outcomes": [
                {"name": "No", "description": "Cote inverse", "price": 1.2},   # pas le bon côté
                {"name": "Yes", "description": "", "price": 3.0},              # sans nom
                {"name": "Yes", "description": "Cote nulle", "price": 1.0},    # cote impossible
                {"name": "Yes", "description": "Valide", "price": 5.0},
            ],
        }]}])
        board = provider.goal_scorers(comp("football", "premier_league"), "A", "B")
        assert [s.player for s in board.scorers] == ["Valide"]

    def test_probabilities_are_not_normalised(self, provider, monkeypatch):
        """Marquer n'est pas une issue exclusive : la somme peut dépasser 100 %."""
        self._repond(provider, monkeypatch, [{"title": "Book", "markets": [{
            "key": "player_goal_scorer_anytime",
            "outcomes": [
                {"name": "Yes", "description": f"J{i}", "price": 2.0} for i in range(4)
            ],
        }]}])
        board = provider.goal_scorers(comp("football", "premier_league"), "A", "B")
        assert sum(s.probability for s in board.scorers) == pytest.approx(2.0)

    def test_distant_match_costs_no_credit(self, tmp_path, monkeypatch):
        """Le marché n'ouvre qu'à l'approche : inutile d'appeler l'API avant."""
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        http = ds.HttpClient(ds.CacheStore(tmp_path / "c.json"), ds.QuotaTracker(tmp_path / "q.json"))
        prov = ds.TheOddsApiProvider(http, api_key="fake")
        monkeypatch.setattr(prov, "resolve_keys", lambda _c: ["soccer_epl"])
        lointain = datetime.now(UTC) + timedelta(days=ds.TheOddsApiProvider.SCORER_MAX_DAYS + 3)
        monkeypatch.setattr(prov, "_events", lambda *_a: ([{
            "id": "e1", "home_team": "A", "away_team": "B",
            "commence_time": lointain.isoformat(),
        }], time.time(), False))

        appels = []
        monkeypatch.setattr(prov.http, "get_json", lambda *a, **k: appels.append(1))
        assert prov.goal_scorers(comp("football", "premier_league"), "A", "B") is None
        assert appels == [], "aucun appel réseau ne doit partir pour un match lointain"

    def test_other_sports_are_ignored(self, provider):
        assert provider.goal_scorers(comp("tennis", "atp_tour"), "A", "B") is None


# ==========================================================================
# Classement reconstruit depuis les résultats openfootball
#
# Les deux fournisseurs de classement exigent une clé payante ou bridée.
# Ce calcul est la seule source de classement disponible sans clé : il doit
# être exact, sous peine de fausser la pondération du modèle.
# ==========================================================================
class TestOpenFootballStandings:
    @pytest.fixture
    def provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        http = ds.HttpClient(ds.CacheStore(tmp_path / "c.json"), ds.QuotaTracker(tmp_path / "q.json"))
        prov = ds.OpenFootballProvider(http)
        prov.enabled = True
        return prov

    @staticmethod
    def _charge(prov, monkeypatch, matches):
        monkeypatch.setattr(
            prov, "_season_file", lambda _c: ({"matches": matches}, time.time(), False, 2025)
        )

    def test_table_is_exact(self, provider, monkeypatch):
        # A bat B 2-0 ; B bat C 1-0 ; A et C font 1-1.
        self._charge(provider, monkeypatch, [
            {"team1": "A", "team2": "B", "score": {"ft": [2, 0]}},
            {"team1": "B", "team2": "C", "score": {"ft": [1, 0]}},
            {"team1": "A", "team2": "C", "score": {"ft": [1, 1]}},
        ])
        table, prov = provider.standings(comp("football", "premier_league"))

        assert table["A"].points == 4 and table["A"].won == 1 and table["A"].drawn == 1
        assert table["B"].points == 3 and table["B"].lost == 1
        assert table["C"].points == 1 and table["C"].lost == 1
        assert table["A"].goals_for == 3 and table["A"].goals_against == 1
        assert [s.team for s in sorted(table.values(), key=lambda x: x.rank)] == ["A", "B", "C"]
        assert "2 matchs joués" not in prov.detail  # 3 matchs joués, pas 2

    def test_totals_balance(self, provider, monkeypatch):
        """Identité comptable : la somme des buts marqués égale celle des encaissés."""
        self._charge(provider, monkeypatch, [
            {"team1": "A", "team2": "B", "score": {"ft": [3, 1]}},
            {"team1": "C", "team2": "A", "score": {"ft": [0, 2]}},
            {"team1": "B", "team2": "C", "score": {"ft": [4, 4]}},
        ])
        table, _ = provider.standings(comp("football", "premier_league"))
        assert sum(s.goals_for for s in table.values()) == sum(s.goals_against for s in table.values())
        assert sum(s.played for s in table.values()) == 6          # 3 matchs × 2 équipes
        assert all(s.won + s.drawn + s.lost == s.played for s in table.values())

    def test_unplayed_matches_are_ignored(self, provider, monkeypatch):
        """Un match sans score ne doit jamais être compté comme un 0-0."""
        self._charge(provider, monkeypatch, [
            {"team1": "A", "team2": "B", "score": {"ft": [1, 0]}},
            {"team1": "A", "team2": "B"},                          # pas encore joué
        ])
        table, _ = provider.standings(comp("football", "premier_league"))
        assert table["A"].played == 1 and table["B"].played == 1

    def test_season_without_a_single_result_returns_none(self, provider, monkeypatch):
        """Plutôt aucun classement qu'un tableau de zéros trompeur."""
        self._charge(provider, monkeypatch, [{"team1": "A", "team2": "B"}])
        assert provider.standings(comp("football", "premier_league")) is None

    def test_cups_have_no_standings(self, provider, monkeypatch):
        self._charge(provider, monkeypatch, [
            {"team1": "A", "team2": "B", "score": {"ft": [1, 0]}},
        ])
        coupe = Competition(
            key="c", label="Coupe", sport="football", openfootball_code="x", is_cup=True
        )
        assert provider.standings(coupe) is None


# ==========================================================================
# Normalisation / rapprochement des noms
# ==========================================================================
class TestNameMatching:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("Real Madrid CF", "Real Madrid"),
            ("FC Barcelona", "Barcelona"),
            ("Paris Saint-Germain", "Paris Saint Germain"),
            ("Bayern München", "Bayern Munchen"),
            ("Manchester Utd", "Manchester Utd FC"),
        ],
    )
    def test_similar_names_match(self, a, b):
        assert ds.name_similarity(a, b) > 0.72

    def test_different_teams_do_not_match(self):
        assert ds.name_similarity("Manchester United", "Manchester City") < 0.9
        assert ds.name_similarity("Real Madrid", "Atletico Madrid") < 0.9

    def test_best_match_respects_threshold(self):
        pool = ["Arsenal", "Aston Villa", "Chelsea"]
        assert ds.best_match("Arsenal FC", pool) == "Arsenal"
        assert ds.best_match("Real Betis", pool) is None

    def test_normalize_strips_noise_and_accents(self):
        assert ds.normalize_name("FC Bayern München") == "bayern munchen"
        assert ds.normalize_name("A.S. Roma") == "roma"


# ==========================================================================
# Cache disque, organisé par compétition (§8)
# ==========================================================================
class TestCacheStore:
    @pytest.fixture
    def store(self, tmp_path):
        return ds.CacheStore(path=tmp_path / "cache.json")

    def test_roundtrip(self, store):
        store.set("k", {"value": 42})
        got = store.get("k", ttl=60)
        assert got is not None and got[0] == {"value": 42}

    def test_ttl_expiry(self, store):
        store.set("k", "vieux")
        store._data["k"]["ts"] = time.time() - 120
        assert store.get("k", ttl=60) is None
        # ... mais reste accessible en mode « quota épuisé »
        assert store.get_stale("k")[0] == "vieux"

    def test_missing_key(self, store):
        assert store.get("inconnu", ttl=60) is None

    def test_persistence_across_instances(self, tmp_path):
        path = tmp_path / "cache.json"
        ds.CacheStore(path=path).set("k", [1, 2, 3])
        assert ds.CacheStore(path=path).get("k", ttl=600)[0] == [1, 2, 3]

    def test_keys_are_stable_and_distinct(self):
        k1 = ds.CacheStore.make_key("url", {"a": 1, "b": 2})
        k2 = ds.CacheStore.make_key("url", {"b": 2, "a": 1})
        k3 = ds.CacheStore.make_key("url", {"a": 9})
        assert k1 == k2 and k1 != k3

    def test_purges_beyond_max_entries(self, tmp_path):
        store = ds.CacheStore(path=tmp_path / "c.json", max_entries=10)
        for i in range(25):
            store.set(f"k{i}", i)
        assert len(store._data) <= 10
        assert store.get("k24", ttl=600) is not None

    def test_scopes_are_isolated_per_competition(self, store):
        epl, liga = comp("football", "premier_league"), comp("football", "la_liga")
        store.set("a", 1, scope=epl.scope)
        store.set("b", 2, scope=epl.scope)
        store.set("c", 3, scope=liga.scope)
        assert store.scopes() == {epl.scope: 2, liga.scope: 1}
        assert store.clear_scope(epl.scope) == 2
        assert store.get("a", ttl=600) is None
        assert store.get("c", ttl=600) is not None  # l'autre compétition est intacte


# ==========================================================================
# Suivi de quota
# ==========================================================================
class TestQuotaTracker:
    @pytest.fixture
    def tracker(self, tmp_path):
        return ds.QuotaTracker(path=tmp_path / "quota.json")

    def test_counts_calls(self, tracker):
        for _ in range(3):
            tracker.record("the_odds_api")
        status = tracker.status("the_odds_api")
        assert status.used == 3
        assert status.remaining == status.limit - 3
        assert not status.exhausted

    def test_headers_take_precedence(self, tracker):
        tracker.record("the_odds_api", 5)
        tracker.set_authoritative("the_odds_api", used=120, remaining=380)
        status = tracker.status("the_odds_api")
        assert status.used == 120 and status.remaining == 380
        assert status.authoritative and status.limit == 500

    def test_warning_and_exhaustion(self, tracker):
        tracker.set_authoritative("the_odds_api", used=495, remaining=5)
        assert tracker.status("the_odds_api").warning
        tracker.set_authoritative("the_odds_api", used=500, remaining=0)
        assert tracker.status("the_odds_api").exhausted
        assert not tracker.can_spend("the_odds_api")

    def test_unknown_provider_is_unlimited(self, tracker):
        assert tracker.status("inconnu") is None
        assert tracker.can_spend("inconnu")

    def test_period_rollover_resets(self, tracker):
        tracker.record("api_football", 40)
        tracker._data["api_football"]["bucket"] = "1999-01-01"
        assert tracker.status("api_football").used == 0

    def test_all_status_covers_declared_quotas(self, tracker):
        assert {s.provider for s in tracker.all_status()} == {q.provider for q in cfg.QUOTAS}


# ==========================================================================
# Modèles de données
# ==========================================================================
class TestModels:
    def _form(self):
        now = datetime.now(UTC)
        matches = [
            ds.MatchResult(now - timedelta(days=3), "X", True, 3, 1),
            ds.MatchResult(now - timedelta(days=10), "Y", False, 0, 2),
            ds.MatchResult(now - timedelta(days=17), "Z", True, 2, 2),
            ds.MatchResult(now - timedelta(days=24), "W", False, 1, 0),
        ]
        return ds.TeamForm("A", "football", matches, ds.Provenance("t", "T", now))

    def test_aggregates(self):
        form = self._form()
        assert form.n == 4
        assert form.scored_avg == pytest.approx(1.5)
        assert form.conceded_avg == pytest.approx(1.25)
        assert form.scored_avg_split(True) == pytest.approx(2.5)
        assert form.scored_avg_split(False) == pytest.approx(0.5)
        assert form.form_string == "WLDW"
        assert form.points_rate == pytest.approx((3 + 0 + 1 + 3) / 12)

    def test_enriched_aggregates(self):
        form = self._form()
        assert form.clean_sheet_rate == pytest.approx(0.25)   # le 1-0
        assert form.btts_rate == pytest.approx(0.5)           # 3-1 et 2-2
        assert form.over_rate(2.5) == pytest.approx(0.5)      # 3-1 et 2-2
        assert form.streak == ("W", 1)
        assert 2.9 < form.rest_days < 3.1

    def test_extra_avg_returns_none_when_absent(self):
        assert self._form().extra_avg("corners_for") is None
        assert self._form().xg_for is None

    def test_provenance_freshness(self):
        fresh = ds.Provenance("s", "S", datetime.now(UTC))
        old = ds.Provenance("s", "S", datetime.now(UTC) - timedelta(days=5))
        assert not fresh.is_stale and fresh.freshness() == "à l'instant"
        assert old.is_stale and "j" in old.freshness()

    def test_odds_snapshot_helpers(self):
        snap = ds.OddsSnapshot(
            home_team="A", away_team="B", commence_time=None, sport_key="k",
            provenance=ds.Provenance("s", "S", datetime.now(UTC)),
            h2h={"A": 1.9, "B": 2.0},
            totals={2.5: {"Over": 1.90, "Under": 1.92}, 3.5: {"Over": 3.10, "Under": 1.35}},
            spreads={-1.5: {"A": 2.60, "B": 1.50}},
        )
        assert snap.has_h2h
        assert snap.main_total_line() == 2.5  # la ligne la plus équilibrée
        line, book = snap.main_spread()
        assert line == -1.5 and "A" in book

    def test_dispersion_measures_bookmaker_disagreement(self):
        base = dict(
            home_team="A", away_team="B", commence_time=None, sport_key="k",
            provenance=ds.Provenance("s", "S", datetime.now(UTC)), h2h={"A": 1.6},
        )
        agree = ds.OddsSnapshot(**base, per_book_h2h={"x": {"A": 1.60}, "y": {"A": 1.62}})
        split = ds.OddsSnapshot(**base, per_book_h2h={"x": {"A": 1.40}, "y": {"A": 1.90}})
        assert agree.dispersion < 0.05 < split.dispersion
        assert ds.OddsSnapshot(**base).dispersion is None

    def test_standing_helpers(self):
        standing = ds.Standing("A", rank=2, played=20, points=44, goals_for=40, goals_against=18)
        assert standing.points_per_game == pytest.approx(2.2)
        assert standing.goal_difference == 22

    def test_weather_summary_and_severity(self):
        calm = ds.WeatherInfo("Londres", 14.0, 0.0, 9.0, ds.Provenance("w", "W", datetime.now(UTC)))
        storm = ds.WeatherInfo("Londres", 6.0, 5.2, 48.0, ds.Provenance("w", "W", datetime.now(UTC)))
        assert "14 °C" in calm.summary() and not calm.is_rough
        assert storm.is_rough


# ==========================================================================
# Historique des cotes
# ==========================================================================
class TestOddsHistory:
    def _snap(self, price):
        return ds.OddsSnapshot(
            home_team="Arsenal", away_team="Chelsea", commence_time=None, sport_key="soccer_epl",
            provenance=ds.Provenance("o", "O", datetime.now(UTC)),
            h2h={"Arsenal": price, "Chelsea": 4.0},
        )

    def test_first_record_has_no_movement(self, tmp_path):
        hist = ds.OddsHistory(path=tmp_path / "odds.json")
        assert hist.record(comp("football", "premier_league"), self._snap(1.80)) == {}

    def test_movement_is_measured_against_the_first_reading(self, tmp_path):
        hist = ds.OddsHistory(path=tmp_path / "odds.json")
        epl = comp("football", "premier_league")
        hist.record(epl, self._snap(2.00))
        drift = hist.record(epl, self._snap(1.80))
        assert drift["movement"]["Arsenal"] == pytest.approx(-0.10)
        assert drift["hours"] >= 0

    def test_events_are_isolated(self, tmp_path):
        hist = ds.OddsHistory(path=tmp_path / "odds.json")
        epl, liga = comp("football", "premier_league"), comp("football", "la_liga")
        hist.record(epl, self._snap(2.00))
        assert hist.record(liga, self._snap(1.80)) == {}  # autre compétition


# ==========================================================================
# Repli entre sources (aucun accès réseau : providers factices)
# ==========================================================================
class _FailingProvider(ds.BaseProvider):
    name, label = "fail", "Source en panne"
    supports = frozenset({"football"})

    def handles(self, comp):
        return True

    def participants(self, comp):
        raise RuntimeError("API HS")

    def form(self, comp, team):
        raise RuntimeError("API HS")


class _EmptyProvider(ds.BaseProvider):
    name, label = "empty", "Source vide"
    supports = frozenset({"football"})

    def handles(self, comp):
        return True


class _WorkingProvider(ds.BaseProvider):
    name, label = "ok", "Source de secours"
    supports = frozenset({"football"})

    def handles(self, comp):
        return True

    def participants(self, comp):
        teams = ["Arsenal", "Chelsea", "Liverpool", "Everton",
                 "Fulham", "Brentford", "Brighton", "Wolves"]
        return teams, self._prov(time.time(), False, comp.label)

    def form(self, comp, team):
        now = datetime.now(UTC)
        return ds.TeamForm(
            team, comp.sport,
            [ds.MatchResult(now, "X", True, 2, 1)],
            self._prov(time.time(), False, comp.label),
        )


class TestFallback:
    @pytest.fixture
    def hub(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        monkeypatch.setattr(cfg, "ODDS_HISTORY_FILE", tmp_path / "o.json")
        hub = ds.DataHub(cfg.ApiKeys(odds_api="", football_data="", rapidapi="", balldontlie=""))
        http = hub.http
        hub.providers = [_FailingProvider(http), _EmptyProvider(http), _WorkingProvider(http)]
        return hub

    def test_broken_source_does_not_break_the_app(self, hub):
        names, provs = hub.participants(comp("football", "premier_league"))
        assert "Arsenal" in names and "Chelsea" in names
        assert provs and provs[0].source == "ok"

    def test_collect_falls_back_and_tracks_provenance(self, hub):
        bundle = hub.collect(
            comp("football", "premier_league"), "Arsenal", "Chelsea",
            with_news=False, with_weather=False,
        )
        assert bundle.form_home is not None and bundle.form_away is not None
        assert bundle.odds is None and "odds_missing" in bundle.notes
        assert all(p.source == "ok" for p in bundle.provenances)
        assert bundle.competition.key == "premier_league"

    def test_league_context_falls_back_to_default(self, hub):
        bundle = hub.collect(
            comp("football", "premier_league"), "Arsenal", "Chelsea",
            with_news=False, with_weather=False,
        )
        # 2 matchs seulement → on garde la moyenne de référence, sans l'inventer.
        assert bundle.league_context["estimated"] is False
        assert bundle.league_context["avg_per_team"] == cfg.ENGINE.league_avg_goals_football

    def test_disabled_sources_are_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        hub = ds.DataHub(cfg.ApiKeys(odds_api="", football_data="", rapidapi="", balldontlie=""))
        missing = hub.missing_keys()
        # Les libellés affichés sont en langage courant, jamais des noms d'API.
        assert "Cotes des bookmakers" in missing   # pas de clé → désactivée
        assert "Données officielles NHL" not in missing  # aucune clé requise
        assert not any("API" in label and "Odds" in label for label in missing)

    def test_sources_are_filtered_by_competition(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "HTTP_CACHE_FILE", tmp_path / "c.json")
        monkeypatch.setattr(cfg, "QUOTA_FILE", tmp_path / "q.json")
        hub = ds.DataHub(cfg.ApiKeys(odds_api="", football_data="", rapidapi="", balldontlie=""))
        nhl_sources = {p.name for p in hub.sources_for(comp("hockey", "nhl"))}
        epl_sources = {p.name for p in hub.sources_for(comp("football", "premier_league"))}
        assert "nhl_api" in nhl_sources
        assert "nhl_api" not in epl_sources


# ==========================================================================
# Historique local des prédictions
# ==========================================================================
class TestHistory:
    def test_append_and_read(self, tmp_path):
        hist = ds.PredictionHistory(path=tmp_path / "h.json", limit=3)
        for i in range(5):
            hist.add({"home": f"A{i}", "away": "B", "favorite": "A", "confidence": 6.0})
        rows = hist.all()
        assert len(rows) == 3 and rows[-1]["home"] == "A4"
        assert "ts" in rows[0]


class TestConsensusPrice:
    """Cette cote sert d'ancrage à toute la méthode no-vig : un bookmaker
    isolé ne doit pas pouvoir déplacer l'ensemble des probabilités."""

    def test_an_isolated_bookmaker_does_not_move_the_consensus(self):
        prix = [2.0] * 10 + [5.0]
        assert sum(prix) / len(prix) == pytest.approx(2.273, abs=0.01)
        assert ds.consensus_price(prix) == pytest.approx(2.0)

    def test_normal_dispersion_is_averaged(self):
        """Des opérateurs sérieux ne s'accordent jamais à la décimale près."""
        assert ds.consensus_price([1.95, 2.00, 2.02, 2.05, 1.98]) == pytest.approx(2.0, abs=0.01)

    def test_tolerance_is_relative_not_absolute(self):
        """0,10 d'écart est énorme sur 1,20 et négligeable sur 12,00."""
        serre = ds.consensus_price([1.20, 1.21, 1.22, 3.00])
        assert serre == pytest.approx(1.21, abs=0.01), "3.00 doit être écarté"
        large = ds.consensus_price([12.0, 12.2, 12.5, 30.0])
        assert 12.0 <= large <= 12.5, "30.0 doit être écarté, pas 12.5"

    def test_a_very_split_market_falls_back_to_the_median(self):
        assert ds.consensus_price([1.5, 2.0, 3.0, 5.0, 9.0]) == pytest.approx(3.0)

    def test_one_or_two_bookmakers_are_kept_as_is(self):
        assert ds.consensus_price([3.3]) == pytest.approx(3.3)
        assert ds.consensus_price([2.0, 2.6]) == pytest.approx(2.3)

    def test_invalid_prices_are_ignored(self):
        assert ds.consensus_price([0.0, 1.0, -3.0, 2.0, 2.0]) == pytest.approx(2.0)
        assert ds.consensus_price([]) == 0.0
