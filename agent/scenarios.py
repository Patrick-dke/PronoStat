"""Module d'exploration des scénarios alternatifs.

Responsabilité unique : ne pas s'arrêter au déroulement le plus probable.

Tous les scénarios sont mesurés **sur les simulations déjà effectuées** : ce
sont des probabilités réelles issues de la même distribution, jamais des
hypothèses ajoutées à la main. Un scénario n'est retenu que s'il est à la fois
plausible et capable de faire tomber la décision.
"""

from __future__ import annotations

import numpy as np

from agent.contracts import Scenario


class ScenarioExplorer:
    """Décompose le risque d'échec du pronostic en déroulements nommés."""

    def explore(self, prediction, pick_key: str | None, limit: int = 3) -> list[Scenario]:
        trace = getattr(prediction, "samples", None)
        sport = prediction.sport
        scenarios: list[Scenario] = []

        if trace is not None and sport in {"football", "hockey"}:
            scenarios = self._goal_sport(prediction, trace, pick_key)
        elif trace is not None and sport == "basket":
            scenarios = self._basket(prediction, trace, pick_key)
        elif trace is not None and sport == "tennis":
            scenarios = self._tennis(prediction, trace, pick_key)

        if not scenarios:
            scenarios = self._fallback(prediction, pick_key)

        scenarios = [s for s in scenarios if 0.02 <= s.probability <= 0.98]
        return sorted(scenarios, key=lambda s: -s.impact)[:limit]

    # ------------------------------------------------------------------
    def _goal_sport(self, prediction, trace, pick_key) -> list[Scenario]:
        home, away = trace["home"], trace["away"]
        diff = home - away
        total = home + away
        n = home.size
        favourite_is_home = prediction.outcome_probs.get("home", 0) >= prediction.outcome_probs.get("away", 0)
        outsider = prediction.away if favourite_is_home else prediction.home
        upset = (diff < 0) if favourite_is_home else (diff > 0)

        out = [
            Scenario(
                key="nul",
                text="Le match se termine sur un nul",
                probability=float(np.mean(diff == 0)),
                breaks_pick=self._breaks(pick_key, {"1x2_home", "1x2_away"}),
            ),
            Scenario(
                key="surprise",
                text=f"{outsider} l'emporte contre toute attente",
                probability=float(np.mean(upset)),
                breaks_pick=self._breaks(pick_key, {"1x2_home", "1x2_away", "ml_home", "ml_away"}),
            ),
            Scenario(
                key="match_ferme",
                text="Rencontre verrouillée, très peu d'occasions concrétisées",
                probability=float(np.mean(total <= 1)),
                breaks_pick=str(pick_key or "").startswith(("total_over", "btts_yes")),
            ),
            Scenario(
                key="match_ouvert",
                text="Match spectaculaire, buts des deux côtés",
                probability=float(np.mean((home >= 2) & (away >= 2))),
                breaks_pick=str(pick_key or "").startswith(("total_under", "btts_no")),
            ),
            Scenario(
                key="victoire_etroite",
                text="Victoire arrachée d'un seul but",
                probability=float(np.mean(np.abs(diff) == 1)),
                breaks_pick=str(pick_key or "").startswith(("hcp_", "puckline_", "spread_")),
            ),
        ]
        if prediction.sport == "hockey":
            out.append(
                Scenario(
                    key="prolongation",
                    text="Le match bascule en prolongation",
                    probability=float(prediction.expected.get("p_overtime", 0.0)),
                    breaks_pick=self._breaks(pick_key, {"1x2_home", "1x2_away"}),
                )
            )
        return out

    def _basket(self, prediction, trace, pick_key) -> list[Scenario]:
        home, away = trace["home"], trace["away"]
        diff = home - away
        total = home + away
        favourite_is_home = prediction.outcome_probs.get("home", 0) >= 0.5
        outsider = prediction.away if favourite_is_home else prediction.home
        upset = (diff < 0) if favourite_is_home else (diff > 0)
        return [
            Scenario(
                key="surprise",
                text=f"{outsider} crée la surprise",
                probability=float(np.mean(upset)),
                breaks_pick=self._breaks(pick_key, {"1x2_home", "1x2_away"}),
            ),
            Scenario(
                key="money_time",
                text="Fin de match serrée, moins de cinq points d'écart",
                probability=float(np.mean(np.abs(diff) <= 5)),
                breaks_pick=str(pick_key or "").startswith("spread_"),
            ),
            Scenario(
                key="rythme_lent",
                text="Match haché et défensif, total sous les attentes",
                probability=float(np.mean(total < np.median(total) - 12)),
                breaks_pick=str(pick_key or "").startswith("total_over"),
            ),
        ]

    def _tennis(self, prediction, trace, pick_key) -> list[Scenario]:
        sets_home, sets_away = trace["home"], trace["away"]
        games = trace.get("games")
        favourite_is_home = prediction.outcome_probs.get("home", 0) >= 0.5
        outsider = prediction.away if favourite_is_home else prediction.home
        upset = (sets_home < sets_away) if favourite_is_home else (sets_home > sets_away)
        out = [
            Scenario(
                key="surprise",
                text=f"{outsider} renverse le match",
                probability=float(np.mean(upset)),
                breaks_pick=self._breaks(pick_key, {"1x2_home", "1x2_away"}),
            ),
            Scenario(
                key="trois_sets",
                text="Le match va au troisième set",
                probability=float(np.mean((sets_home + sets_away) >= 3)),
                breaks_pick=str(pick_key or "").startswith("sets_"),
            ),
        ]
        if games is not None:
            out.append(
                Scenario(
                    key="marathon",
                    text="Rencontre longue, nombreux jeux disputés",
                    probability=float(np.mean(games >= np.median(games) + 4)),
                    breaks_pick=str(pick_key or "").startswith("total_under"),
                )
            )
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _breaks(pick_key: str | None, keys: set[str]) -> bool:
        return bool(pick_key) and pick_key in keys

    def _fallback(self, prediction, pick_key) -> list[Scenario]:
        """Sans simulations disponibles, on s'appuie sur les probabilités calculées."""
        pick = next((l for l in prediction.lines if l.key == pick_key), None)
        if pick is None:
            return []
        return [
            Scenario(
                key="echec_pronostic",
                text="Le pronostic ne se réalise pas",
                probability=max(0.0, 1.0 - pick.prob),
                breaks_pick=True,
            )
        ]
