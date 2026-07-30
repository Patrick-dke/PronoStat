"""Tests des fonctions d'interface qui ne dépendent pas du rendu Streamlit."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402

streamlit = pytest.importorskip("streamlit", reason="Streamlit requis pour l'interface")
import app  # noqa: E402


TEAMS = ["Arsenal", "Chelsea", "Liverpool", "Manchester City"]


class TestOpponentSelection:
    def test_selected_team_disappears_from_the_other_list(self):
        assert app.opponent_options(TEAMS, "Arsenal") == [
            "Chelsea", "Liverpool", "Manchester City"
        ]

    def test_order_is_preserved(self):
        assert app.opponent_options(TEAMS, "Liverpool") == [
            "Arsenal", "Chelsea", "Manchester City"
        ]

    def test_no_selection_keeps_everything(self):
        assert app.opponent_options(TEAMS, None) == TEAMS

    def test_unknown_selection_changes_nothing(self):
        assert app.opponent_options(TEAMS, "Real Madrid") == TEAMS

    def test_empty_entries_are_dropped(self):
        assert app.opponent_options(["Arsenal", "", None], "Arsenal") == []

    def test_single_team_leaves_no_opponent(self):
        """Aucun adversaire possible : le bouton doit rester désactivé."""
        assert app.opponent_options(["Arsenal"], "Arsenal") == []


class TestFrenchWording:
    def test_elision_before_a_vowel(self):
        assert app.de("Arsenal") == "d'Arsenal"
        assert app.de("Everton") == "d'Everton"
        assert app.de("Inter Milan") == "d'Inter Milan"

    def test_no_elision_before_a_consonant(self):
        assert app.de("Chelsea") == "de Chelsea"
        assert app.de("Boston Bruins") == "de Boston Bruins"

    def test_accented_initial_is_handled(self):
        assert app.de("Étoile Rouge") == "d'Étoile Rouge"


class TestFormatting:
    def test_percentages(self):
        assert app.pct(0.6667) == "67 %"
        assert app.pct(0.6667, 1) == "66.7 %"
        assert app.pct(None) == "—"

    def test_numbers_use_french_decimal_comma(self):
        assert app.num(2.5) == "2,5"
        assert app.num(12.34, 2) == "12,34"
        assert app.num(55.0, 0, " %") == "55 %"
        assert app.num(None) == "—"

    def test_html_is_escaped(self):
        assert "&lt;script&gt;" in app.esc("<script>")
        assert "<b>x</b>" in app.card("Titre", "<b>x</b>")   # le corps reste du HTML
        assert "&lt;b&gt;" in app.card("<b>", "")            # le titre est échappé


class TestPublicWording:
    """L'interface ne doit jamais exposer de nom technique d'API."""

    FORBIDDEN = ("The Odds API", "API-Football", "balldontlie", "TheSportsDB",
                 "football-data.org", "no-vig", "Monte Carlo", "TTL", "cache TTL")

    def test_source_names_are_plain_language(self):
        for source in cfg.SOURCE_RELIABILITY:
            name = cfg.public_name(source)
            assert name
            assert not any(bad.lower() in name.lower() for bad in self.FORBIDDEN)

    def test_quota_labels_are_plain_language(self):
        for rule in cfg.QUOTAS:
            assert not any(bad.lower() in rule.label.lower() for bad in self.FORBIDDEN)

    def test_unknown_source_has_a_readable_fallback(self):
        assert cfg.public_name("source_inconnue") == "Source sportive"
