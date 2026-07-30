"""Module de raisonnement multicritère.

Responsabilité unique : transformer les données brutes en **facteurs
comparables**, chacun signé, pondéré et assorti d'un niveau de confiance.

Deux usages distincts, volontairement séparés :

  * **expliquer** — tous les facteurs alimentent la justification affichée ;
  * **décider**   — seuls les facteurs *non consommés par la simulation*
    ajustent la probabilité finale. La forme récente, par exemple, est déjà
    dans les forces d'équipe : la réappliquer reviendrait à la compter deux
    fois.

Un facteur dont la donnée manque est marqué indisponible ; il n'est ni
inventé, ni remplacé par une valeur neutre arbitraire.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import config as cfg
from agent.contracts import ConfiguredWeights, Factor, FactorReport
from data_sources import Bundle, TeamForm

UTC = timezone.utc


def _squash(x: float, scale: float) -> float:
    """Ramène un écart brut dans [-1, 1] sans effet de seuil brutal."""
    if scale <= 0:
        return 0.0
    return math.tanh(x / scale)


def _sample_confidence(n: int, target: int = 8) -> float:
    """Confiance croissante avec la taille d'échantillon, plafonnée à 1."""
    return min(1.0, n / target) if n > 0 else 0.0


class FactorEngine:
    """Évalue chaque critère du raisonnement pour un match donné."""

    def __init__(self, policy=None):
        self.policy = policy or ConfiguredWeights()
        self._specs = ConfiguredWeights.specs()
        self._context_specs = {s.key: s for s in cfg.CONTEXT_FACTORS}

    # ------------------------------------------------------------------
    def evaluate(self, bundle: Bundle, prediction=None, report=None) -> FactorReport:
        weights = self.policy.weights({"sport": bundle.sport,
                                       "competition": getattr(bundle.competition, "key", None)})
        out = FactorReport()
        home, away = bundle.form_home, bundle.form_away

        add = lambda f: out.factors.append(f)  # noqa: E731
        add(self._forme_recente(home, away, weights))
        add(self._confrontations(bundle, weights))
        add(self._domicile_exterieur(home, away, weights))
        add(self._efficacite_offensive(bundle, home, away, weights))
        add(self._solidite_defensive(bundle, home, away, weights))
        add(self._classement(bundle, weights))
        add(self._recuperation(home, away, weights))
        add(self._consensus_marche(prediction, weights))
        add(self._dynamique(home, away, weights))
        add(self._calendrier(home, away, weights))
        add(self._importance(bundle, weights))
        add(self._evolution_cotes(bundle, prediction, weights))

        out.context = self._context(bundle, report)
        return out

    # ------------------------------------------------------------------
    # Facteurs déjà consommés par la simulation (explicatifs)
    # ------------------------------------------------------------------
    def _make(self, key: str, value, confidence: float, detail: str,
              weights: dict) -> Factor:
        spec = self._specs[key]
        available = value is not None and confidence > 0
        return Factor(
            key=key,
            label=spec.label,
            value=float(value) if available else 0.0,
            weight=weights.get(key, spec.weight),
            confidence=confidence if available else 0.0,
            detail=detail if available else "donnée indisponible",
            in_model=spec.in_model,
            available=available,
        )

    def _forme_recente(self, home, away, weights) -> Factor:
        if not home or not away or not home.n or not away.n:
            return self._make("forme_recente", None, 0.0, "", weights)
        rate_home, rate_away = home.points_rate, away.points_rate
        if rate_home is None or rate_away is None:
            return self._make("forme_recente", None, 0.0, "", weights)
        value = _squash(rate_home - rate_away, 0.35)
        conf = _sample_confidence(min(home.n, away.n))
        detail = f"{home.form_string or '—'} contre {away.form_string or '—'}"
        return self._make("forme_recente", value, conf, detail, weights)

    def _confrontations(self, bundle: Bundle, weights) -> Factor:
        h2h = bundle.h2h
        if not h2h:
            return self._make("confrontations", None, 0.0, "", weights)
        diffs = [m.scored - m.conceded for m in h2h]
        value = _squash(sum(diffs) / len(diffs), 1.5)
        conf = _sample_confidence(len(h2h), target=5)
        wins = sum(1 for m in h2h if m.outcome == "W")
        detail = f"{wins} victoire(s) sur {len(h2h)} rencontre(s)"
        return self._make("confrontations", value, conf, detail, weights)

    def _domicile_exterieur(self, home, away, weights) -> Factor:
        if not home or not away:
            return self._make("domicile_exterieur", None, 0.0, "", weights)
        home_perf = home.scored_avg_split(True)
        home_conc = home.conceded_avg_split(True)
        away_perf = away.scored_avg_split(False)
        away_conc = away.conceded_avg_split(False)
        if None in (home_perf, home_conc, away_perf, away_conc):
            return self._make("domicile_exterieur", None, 0.0, "", weights)
        diff = (home_perf - home_conc) - (away_perf - away_conc)
        n = min(len(home.split(True)), len(away.split(False)))
        value = _squash(diff, 1.5)
        detail = (
            f"{home_perf:.1f}–{home_conc:.1f} à domicile, "
            f"{away_perf:.1f}–{away_conc:.1f} à l'extérieur"
        ).replace(".", ",")
        return self._make("domicile_exterieur", value, _sample_confidence(n, 4),
                          detail, weights)

    def _league_average(self, bundle: Bundle) -> float:
        ctx = bundle.league_context or {}
        return float(ctx.get("avg_per_team") or 1.4) or 1.4

    def _efficacite_offensive(self, bundle, home, away, weights) -> Factor:
        if not home or not away:
            return self._make("efficacite_offensive", None, 0.0, "", weights)
        # Les xG priment sur les buts quand la source les fournit.
        h = home.xg_for if home.xg_for is not None else home.scored_avg
        a = away.xg_for if away.xg_for is not None else away.scored_avg
        if h is None or a is None:
            return self._make("efficacite_offensive", None, 0.0, "", weights)
        avg = self._league_average(bundle)
        value = _squash((h - a) / max(avg, 0.1), 0.9)
        basis = "occasions créées" if home.xg_for is not None else "buts marqués"
        detail = f"{h:.2f} contre {a:.2f} ({basis})".replace(".", ",")
        return self._make("efficacite_offensive", value,
                          _sample_confidence(min(home.n, away.n)), detail, weights)

    def _solidite_defensive(self, bundle, home, away, weights) -> Factor:
        if not home or not away:
            return self._make("solidite_defensive", None, 0.0, "", weights)
        h = home.xg_against if home.xg_against is not None else home.conceded_avg
        a = away.xg_against if away.xg_against is not None else away.conceded_avg
        if h is None or a is None:
            return self._make("solidite_defensive", None, 0.0, "", weights)
        avg = self._league_average(bundle)
        # Encaisser moins que l'adversaire est un avantage → signe inversé.
        value = _squash((a - h) / max(avg, 0.1), 0.9)
        detail = f"{h:.2f} contre {a:.2f} encaissés".replace(".", ",")
        return self._make("solidite_defensive", value,
                          _sample_confidence(min(home.n, away.n)), detail, weights)

    def _classement(self, bundle: Bundle, weights) -> Factor:
        s_home = bundle.standing(bundle.home)
        s_away = bundle.standing(bundle.away)
        if s_home is None or s_away is None:
            return self._make("classement", None, 0.0, "", weights)
        ppg_home = s_home.points_per_game or 0.0
        ppg_away = s_away.points_per_game or 0.0
        value = _squash(ppg_home - ppg_away, 0.6)
        played = min(s_home.played, s_away.played)
        detail = f"{s_home.rank}ᵉ contre {s_away.rank}ᵉ"
        return self._make("classement", value, _sample_confidence(played, 15),
                          detail, weights)

    def _recuperation(self, home, away, weights) -> Factor:
        if not home or not away:
            return self._make("recuperation", None, 0.0, "", weights)
        rest_home, rest_away = home.rest_days, away.rest_days
        if rest_home is None or rest_away is None:
            return self._make("recuperation", None, 0.0, "", weights)
        # Au-delà d'une semaine, un jour de plus n'apporte rien : on plafonne.
        capped_home = min(rest_home, 8.0)
        capped_away = min(rest_away, 8.0)
        value = _squash(capped_home - capped_away, 3.0)
        detail = f"{capped_home:.0f} contre {capped_away:.0f} jours de repos"
        return self._make("recuperation", value, 0.8, detail, weights)

    def _consensus_marche(self, prediction, weights) -> Factor:
        market = getattr(prediction, "market_probs", None) if prediction else None
        if not market:
            return self._make("consensus_marche", None, 0.0, "", weights)
        value = _squash(market.get("home", 0.0) - market.get("away", 0.0), 0.45)
        books = getattr(prediction, "bookmaker_count", 0) or 0
        detail = f"{market.get('home', 0):.0%} contre {market.get('away', 0):.0%}"
        return self._make("consensus_marche", value,
                          _sample_confidence(books, 5), detail, weights)

    # ------------------------------------------------------------------
    # Facteurs NON consommés par la simulation (ils ajustent la décision)
    # ------------------------------------------------------------------
    def _dynamique(self, home, away, weights) -> Factor:
        if not home or not away or not home.n or not away.n:
            return self._make("dynamique", None, 0.0, "", weights)

        def momentum(form: TeamForm) -> float:
            kind, length = form.streak
            sign = {"W": 1.0, "L": -1.0}.get(kind, 0.0)
            return sign * min(length, 4) / 4.0

        value = _squash(momentum(home) - momentum(away), 0.9)
        detail = f"série {home.streak[1]}{home.streak[0]} / {away.streak[1]}{away.streak[0]}"
        return self._make("dynamique", value,
                          _sample_confidence(min(home.n, away.n), 5), detail, weights)

    def _calendrier(self, home, away, weights) -> Factor:
        """Charge récente : un calendrier dense use, un calendrier vide rouille."""
        if not home or not away:
            return self._make("calendrier", None, 0.0, "", weights)
        now = datetime.now(UTC)

        def load(form: TeamForm) -> int:
            return sum(1 for m in form.matches if (now - m.date).days <= 21)

        load_home, load_away = load(home), load(away)
        if load_home == 0 and load_away == 0:
            return self._make("calendrier", None, 0.0, "", weights)
        # Jouer davantage sur trois semaines pèse légèrement en défaveur.
        value = _squash(load_away - load_home, 2.5)
        detail = f"{load_home} contre {load_away} matchs en trois semaines"
        return self._make("calendrier", value, 0.7, detail, weights)

    def _importance(self, bundle: Bundle, weights) -> Factor:
        """Enjeu : une équipe qui joue sa place se transcende plus souvent.

        Mesuré uniquement là où c'est objectivable — un classement serré en
        haut ou en bas de tableau. Sans classement, le facteur est absent.
        """
        s_home = bundle.standing(bundle.home)
        s_away = bundle.standing(bundle.away)
        if s_home is None or s_away is None:
            return self._make("importance", None, 0.0, "", weights)
        total = len(bundle.standings) or 20

        def stake(rank: int) -> float:
            """1 quand l'équipe joue un objectif (podium ou maintien)."""
            top = 1.0 - min(1.0, (rank - 1) / max(1, total * 0.25))
            bottom = 1.0 - min(1.0, (total - rank) / max(1, total * 0.25))
            return max(top, bottom)

        stake_home, stake_away = stake(s_home.rank), stake(s_away.rank)
        if max(stake_home, stake_away) < 0.3:
            return self._make("importance", None, 0.0, "", weights)
        value = _squash(stake_home - stake_away, 0.8) * 0.5
        detail = "enjeu de classement marqué" if abs(value) > 0.05 else "enjeu comparable"
        return self._make("importance", value, 0.6, detail, weights)

    def _evolution_cotes(self, bundle: Bundle, prediction, weights) -> Factor:
        movement = getattr(prediction, "odds_movement", None) or {}
        if not movement or bundle.odds is None:
            return self._make("evolution_cotes", None, 0.0, "", weights)
        home_name, away_name = bundle.odds.home_team, bundle.odds.away_team
        drift_home = drift_away = None
        for label, delta in movement.items():
            if label == home_name:
                drift_home = delta
            elif label == away_name:
                drift_away = delta
        if drift_home is None and drift_away is None:
            return self._make("evolution_cotes", None, 0.0, "", weights)
        # Une cote qui baisse traduit de l'argent sur cette issue.
        signal = -(drift_home or 0.0) + (drift_away or 0.0)
        value = _squash(signal, 0.10)
        hours = getattr(prediction, "odds_movement_hours", None) or 0
        detail = f"cotes en mouvement depuis {hours:.0f} h"
        return self._make("evolution_cotes", value, 0.7, detail, weights)

    # ------------------------------------------------------------------
    # Facteurs de contexte : ils ne penchent pour personne
    # ------------------------------------------------------------------
    def _context(self, bundle: Bundle, report) -> list[Factor]:
        found = len(getattr(report, "fields_found", []) or [])
        missing = len(getattr(report, "fields_missing", []) or [])
        total = found + missing
        availability = found / total if total else 0.0
        quality = float(getattr(report, "reliability", 0.0) or 0.0)

        def ctx(key: str, value: float, detail: str) -> Factor:
            spec = self._context_specs[key]
            return Factor(
                key=key, label=spec.label, value=0.0, weight=spec.weight,
                confidence=value, detail=detail, in_model=False,
                available=value > 0,
            )

        return [
            ctx("disponibilite_donnees", availability,
                f"{found} information(s) sur {total or '—'} recherchées"),
            ctx("qualite_sources", quality, f"indice de fiabilité {quality:.0%}"),
        ]
