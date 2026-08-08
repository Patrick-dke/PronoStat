"""API HTTP : authentification, contrat des routes, gestion des erreurs.

Aucune analyse réelle n'est lancée : le pipeline est remplacé par une
doublure. Ces tests portent sur la couture HTTP, pas sur le moteur, qui a
déjà sa propre suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
import config as cfg  # noqa: E402

JETON = "jeton-de-test"
AUTH = {"Authorization": f"Bearer {JETON}"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PRONOSTAT_API_TOKEN", JETON)
    return TestClient(api.app)


class TestAuthentification:
    def test_health_is_open(self, client):
        """Les sondes de l'hébergeur ne portent pas de jeton."""
        r = client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_a_protected_route_refuses_without_token(self, client):
        assert client.get("/quota").status_code == 401

    def test_a_wrong_token_is_refused(self, client):
        r = client.get("/quota", headers={"Authorization": "Bearer faux"})
        assert r.status_code == 401

    def test_without_configuration_the_service_stays_closed(self, monkeypatch):
        """Un oubli de configuration doit fermer le service, pas l'ouvrir :
        cette API dépense un quota payant."""
        monkeypatch.delenv("PRONOSTAT_API_TOKEN", raising=False)
        r = TestClient(api.app).get("/quota", headers=AUTH)
        assert r.status_code == 503
        assert "TOKEN" in r.json()["detail"]


class TestQuota:
    def test_quota_reports_each_provider(self, client):
        corps = client.get("/quota", headers=AUTH).json()
        assert corps["cost_per_analysis"] == 3
        noms = {p["provider"] for p in corps["providers"]}
        assert "the_odds_api" in noms
        for p in corps["providers"]:
            assert {"provider", "remaining", "period", "exhausted"} <= set(p)


class TestAnalysis:
    @staticmethod
    def _doublure(monkeypatch):
        """Remplace le pipeline : ces tests ne doivent consommer aucun crédit."""
        import engine
        from agent.contracts import Decision
        from agent.pipeline import AnalysisResult

        pred = engine.Prediction(
            sport="football", home="Arsenal", away="Coventry City", n_sims=10,
            outcome_probs={"home": 0.71, "draw": 0.19, "away": 0.10},
            market_probs=None, blended_target=None,
        )
        pred.top_scores = [("1-1", 0.11)]
        pred.pick_scores = [("2-0", 0.13)]
        pred.lines = [engine.MarketLine("1x2_home", "Arsenal", 0.71)]
        pred.consistency = []

        class FauxAgent:
            def analyse_match(self, comp, home, away, record=True):
                return AnalysisResult(
                    decision=Decision(recommendation="Arsenal gagne", market="vainqueur",
                                      probability=0.71, confidence=7.8),
                    prediction=pred, bundle=_FauxBundle(), factors=_Vide(),
                    market=_Marche(), validation=_Validation(),
                )

        monkeypatch.setattr(api, "get_agent", lambda: FauxAgent())

    def test_an_unknown_competition_returns_404(self, client):
        r = client.post("/analysis", headers=AUTH, json={
            "sport": "football", "competition_key": "inexistante",
            "home": "A", "away": "B",
        })
        assert r.status_code == 404

    def test_the_response_carries_the_full_contract(self, client, monkeypatch):
        self._doublure(monkeypatch)
        corps = client.post("/analysis", headers=AUTH, json={
            "sport": "football", "competition_key": "premier_league",
            "home": "Arsenal", "away": "Coventry City",
        }).json()
        for champ in ("analysis_id", "model_version", "prediction_timestamp",
                      "top_scores", "pick_scores", "markets", "confidence",
                      "consistency", "sources"):
            assert champ in corps, f"{champ} manque au contrat"

    def test_the_coherent_score_is_exposed_alongside_the_raw_one(self, client, monkeypatch):
        """n8n doit voir la même chose que l'interface."""
        self._doublure(monkeypatch)
        corps = client.post("/analysis", headers=AUTH, json={
            "sport": "football", "competition_key": "premier_league",
            "home": "Arsenal", "away": "Coventry City",
        }).json()
        assert corps["pick_scores"][0]["score"] == "2-0"
        assert corps["top_scores"][0]["score"] == "1-1"

    def test_research_is_echoed_not_silently_swallowed(self, client, monkeypatch):
        """Le champ est archivé, pas exploité : le renvoyer le rend vérifiable."""
        self._doublure(monkeypatch)
        envoye = {"injuries": {"status": "non_publie"}}
        corps = client.post("/analysis", headers=AUTH, json={
            "sport": "football", "competition_key": "premier_league",
            "home": "Arsenal", "away": "Coventry City", "research": envoye,
        }).json()
        assert corps["research_echo"] == envoye

    def test_an_engine_failure_becomes_a_502(self, client, monkeypatch):
        class AgentCasse:
            def analyse_match(self, *a, **k):
                raise RuntimeError("source injoignable")

        monkeypatch.setattr(api, "get_agent", lambda: AgentCasse())
        r = client.post("/analysis", headers=AUTH, json={
            "sport": "football", "competition_key": "premier_league",
            "home": "Arsenal", "away": "Coventry City",
        })
        assert r.status_code == 502


class TestResult:
    def test_an_unknown_analysis_returns_404(self, client):
        r = client.post("/result", headers=AUTH, json={
            "analysis_id": "inconnu", "home_goals": 2, "away_goals": 0,
        })
        assert r.status_code == 404

    def test_negative_scores_are_rejected(self, client):
        r = client.post("/result", headers=AUTH, json={
            "analysis_id": "x", "home_goals": -1, "away_goals": 0,
        })
        assert r.status_code == 422

    def test_reading_an_unknown_analysis_returns_404(self, client):
        assert client.get("/analysis/inconnu", headers=AUTH).status_code == 404


# --- doublures minimales -------------------------------------------------
class _Vide:
    pass


class _FauxBundle:
    competition = None


class _Marche:
    available = False
    bookmakers = 0
    probabilities: dict = {}


class _Validation:
    usable = True
    discarded: list = []
