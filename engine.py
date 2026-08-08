"""Moteur d'analyse de PronoStat (§6 du cahier des charges).

Chaîne de traitement, exécutée intégralement en arrière-plan :

    1. cotes réelles          → probabilités NO-VIG (§6.2)
    2. forme récente          → forces offensives / défensives (§6.3)
    3. calibration            → mélange marché/modèle par optimisation
                                type Nelder-Mead (§6.4)
    4. Monte Carlo ≥ 10 000   → distribution complète des scores (§6.5)
    5. marchés dérivés        → 1X2, O/U, BTTS, handicap, sets… (§6.6)
    6. détection de valeur    → écart modèle / marché (§6.7)
    7. scénario imprévu       → une ligne (§6.8)
    8. niveau de confiance    → /10 (§6.9)

Aucune valeur n'est inventée : quand une entrée manque, le champ vaut `None`
et l'interface affiche « données indisponibles ».
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timezone
from typing import Callable, Sequence

import numpy as np

import config as cfg
from config import Competition
from data_sources import (
    Bundle,
    MatchResult,
    Provenance,
    Standing,
    TeamForm,
    name_similarity,
)

UTC = timezone.utc


# ==========================================================================
# 1. NO-VIG — retrait de la marge du bookmaker (§6.2)
# ==========================================================================
def implied_probabilities(odds: dict[str, float]) -> dict[str, float]:
    """Probabilité implicite brute de chaque issue : 1 / cote."""
    return {k: 1.0 / v for k, v in odds.items() if v and v > 1.0}


def book_margin(odds: dict[str, float]) -> float | None:
    """Marge du bookmaker : somme des probabilités implicites − 1."""
    implied = implied_probabilities(odds)
    if len(implied) < 2:
        return None
    return sum(implied.values()) - 1.0


def remove_vig(odds: dict[str, float], method: str = "multiplicative") -> dict[str, float] | None:
    """Convertit des cotes décimales en probabilités « pures » sommant à 1.

    `multiplicative` (défaut, méthode décrite au §6.2) : on divise chaque
    probabilité implicite par leur somme.
    `power` : variante qui répartit la marge de façon moins uniforme
    (les gros favoris sont moins pénalisés). Disponible pour extension.
    """
    implied = implied_probabilities(odds)
    if len(implied) < 2:
        return None
    total = sum(implied.values())
    if total <= 0:
        return None

    if method == "multiplicative":
        return {k: v / total for k, v in implied.items()}

    if method == "power":
        # On cherche k tel que somme(p_i^k) == 1.
        def f(k: float) -> float:
            return sum(p ** k for p in implied.values()) - 1.0

        lo, hi = 0.2, 5.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if f(mid) > 0:
                lo = mid
            else:
                hi = mid
        k = (lo + hi) / 2
        raw = {key: p ** k for key, p in implied.items()}
        s = sum(raw.values())
        return {key: v / s for key, v in raw.items()}

    raise ValueError(f"méthode no-vig inconnue : {method}")


def map_h2h_odds(
    h2h: dict[str, float], home_team: str, away_team: str
) -> dict[str, float] | None:
    """Associe les libellés de cotes aux clés canoniques home/draw/away."""
    if not h2h:
        return None
    out: dict[str, float] = {}
    for label, price in h2h.items():
        low = label.strip().lower()
        if low in {"draw", "tie", "nul", "match nul"}:
            out["draw"] = price
            continue
        s_home = name_similarity(label, home_team)
        s_away = name_similarity(label, away_team)
        if s_home >= s_away and s_home > 0.55:
            out["home"] = price
        elif s_away > 0.55:
            out["away"] = price
    return out if len(out) >= 2 else None


# ==========================================================================
# 2. Optimiseur Nelder-Mead (simplexe) — utilisé pour la calibration (§6.4)
# ==========================================================================
def nelder_mead(
    func: Callable[[np.ndarray], float],
    x0: Sequence[float],
    step: float = 0.08,
    max_iter: int = 400,
    tol: float = 1e-10,
) -> np.ndarray:
    """Minimisation sans dérivée par la méthode du simplexe de Nelder-Mead."""
    x0 = np.asarray(x0, dtype=float)
    n = x0.size
    alpha, gamma, rho, sigma = 1.0, 2.0, 0.5, 0.5

    simplex = [x0.copy()]
    for i in range(n):
        pt = x0.copy()
        pt[i] += step * (abs(pt[i]) if pt[i] else 1.0)
        simplex.append(pt)
    values = [func(p) for p in simplex]

    for _ in range(max_iter):
        order = np.argsort(values)
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        if abs(values[-1] - values[0]) < tol:
            break

        centroid = np.mean(simplex[:-1], axis=0)
        reflected = centroid + alpha * (centroid - simplex[-1])
        f_ref = func(reflected)

        if values[0] <= f_ref < values[-2]:
            simplex[-1], values[-1] = reflected, f_ref
            continue
        if f_ref < values[0]:
            expanded = centroid + gamma * (reflected - centroid)
            f_exp = func(expanded)
            if f_exp < f_ref:
                simplex[-1], values[-1] = expanded, f_exp
            else:
                simplex[-1], values[-1] = reflected, f_ref
            continue

        contracted = centroid + rho * (simplex[-1] - centroid)
        f_con = func(contracted)
        if f_con < values[-1]:
            simplex[-1], values[-1] = contracted, f_con
            continue

        best = simplex[0]
        simplex = [best] + [best + sigma * (p - best) for p in simplex[1:]]
        values = [func(p) for p in simplex]

    order = int(np.argmin(values))
    return simplex[order]


# ==========================================================================
# 3. Lois de probabilité (sans dépendance à scipy)
# ==========================================================================
def poisson_pmf(k: np.ndarray, lam: float) -> np.ndarray:
    k = np.asarray(k, dtype=float)
    lam = max(float(lam), 1e-6)
    with np.errstate(divide="ignore"):
        log_p = -lam + k * math.log(lam) - _log_factorial(k)
    return np.exp(log_p)


def _log_factorial(k: np.ndarray) -> np.ndarray:
    from math import lgamma

    return np.array([lgamma(float(x) + 1.0) for x in np.ravel(k)]).reshape(np.shape(k))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Quantile de la loi normale (approximation d'Acklam, |erreur| < 1e-9)."""
    p = min(max(p, 1e-12), 1 - 1e-12)
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ==========================================================================
# 4. Matrice de scores Poisson bivariée + ajustement Dixon-Coles (§6.5)
# ==========================================================================
def dixon_coles_matrix(
    lam_home: float, lam_away: float, rho: float | None = None, max_goals: int | None = None
) -> np.ndarray:
    """Loi jointe des scores (i,j) avec correction Dixon-Coles des petits scores."""
    rho = cfg.ENGINE.dixon_coles_rho if rho is None else rho
    max_goals = cfg.ENGINE.max_goals if max_goals is None else max_goals
    lam_home = max(float(lam_home), 1e-4)
    lam_away = max(float(lam_away), 1e-4)

    grid = np.arange(max_goals + 1)
    ph = poisson_pmf(grid, lam_home)
    pa = poisson_pmf(grid, lam_away)
    matrix = np.outer(ph, pa)

    # τ de Dixon-Coles : ne touche que 0-0, 0-1, 1-0, 1-1.
    matrix[0, 0] *= 1.0 - lam_home * lam_away * rho
    matrix[0, 1] *= 1.0 + lam_home * rho
    matrix[1, 0] *= 1.0 + lam_away * rho
    matrix[1, 1] *= 1.0 - rho

    matrix = np.clip(matrix, 1e-15, None)
    return matrix / matrix.sum()


def matrix_outcome_probs(matrix: np.ndarray) -> tuple[float, float, float]:
    """(victoire domicile, nul, victoire extérieur) à partir de la grille."""
    home = float(np.tril(matrix, -1).sum())
    draw = float(np.trace(matrix))
    away = float(np.triu(matrix, 1).sum())
    return home, draw, away


def sample_scores(matrix: np.ndarray, n: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Tirage Monte Carlo de n scores dans la loi jointe."""
    flat = matrix.ravel()
    flat = flat / flat.sum()
    draws = rng.choice(flat.size, size=n, p=flat)
    return np.divmod(draws, matrix.shape[1])


# ==========================================================================
# 5. Forces d'équipe (§6.3)
# ==========================================================================
@dataclass
class Strength:
    attack: float          # buts marqués / moyenne de référence
    defense: float         # buts encaissés / moyenne de référence
    sample: int
    scored_avg: float | None
    conceded_avg: float | None
    home_sample: int = 0
    away_sample: int = 0
    signals: list[str] = field(default_factory=list)   # signaux réellement utilisés


def rest_multiplier(form: TeamForm | None) -> float:
    """Pénalité de fatigue : < 1 quand l'équipe a peu récupéré (§9).

    Effet volontairement faible et borné (`REST_PENALTY_MAX`), et appliqué
    avant la calibration marché — le consensus reste dominant.
    """
    if form is None:
        return 1.0
    rest = form.rest_days
    if rest is None:
        return 1.0
    ref = max(0.5, cfg.ENGINE.rest_reference_days)
    deficit = max(0.0, min(1.0, (ref - rest) / ref))
    return 1.0 - cfg.ENGINE.rest_penalty_max * deficit


def compute_strength(
    form: TeamForm | None,
    league_avg: float,
    at_home: bool,
    standing: Standing | None = None,
) -> Strength | None:
    """Force offensive/défensive relative.

    Signaux combinés, chacun utilisé **uniquement s'il existe réellement** :
      * buts marqués/encaissés sur les derniers matchs ;
      * split domicile / extérieur ;
      * expected goals (xG) quand l'API les fournit — meilleur prédicteur
        que les buts, mélangé selon `XG_WEIGHT` ;
      * moyennes de la saison complète issues du classement (`STANDINGS_WEIGHT`),
        qui apportent un échantillon bien plus large.
    """
    if form is None or form.n == 0 or league_avg <= 0:
        return None

    overall_scored = form.scored_avg
    overall_conceded = form.conceded_avg
    if overall_scored is None or overall_conceded is None:
        return None

    signals = ["buts"]

    # --- 1. xG : signal plus stable que les buts ---
    xg_for, xg_against = form.xg_for, form.xg_against
    n_xg = min(form.extra_count("xg_for"), form.extra_count("xg_against"))
    if xg_for is not None and xg_against is not None and n_xg >= 3:
        w_xg = max(0.0, min(1.0, cfg.ENGINE.xg_weight))
        overall_scored = (1 - w_xg) * overall_scored + w_xg * xg_for
        overall_conceded = (1 - w_xg) * overall_conceded + w_xg * xg_against
        signals.append(f"xG ({n_xg} matchs)")

    # --- 2. split domicile / extérieur ---
    split_scored = form.scored_avg_split(at_home)
    split_conceded = form.conceded_avg_split(at_home)
    n_split = len(form.split(at_home))
    w = min(0.6, 0.15 * n_split)
    scored = overall_scored if split_scored is None else (1 - w) * overall_scored + w * split_scored
    conceded = (
        overall_conceded
        if split_conceded is None
        else (1 - w) * overall_conceded + w * split_conceded
    )
    if n_split:
        signals.append("domicile" if at_home else "extérieur")

    # --- 3. saison complète (classement) : échantillon bien plus large ---
    # Mélange pondéré par les tailles d'échantillon : `STANDINGS_WEIGHT` dit
    # combien vaut un match de la saison face à un match récent (0.20 = un
    # cinquième). Une saison de 30 matchs pèse donc 6 « matchs récents ».
    season_games = 0.0
    if standing is not None and standing.played >= 5:
        k = max(0.0, min(1.0, cfg.ENGINE.standings_weight))
        season_games = standing.played * k
        w_st = season_games / (season_games + form.n) if season_games else 0.0
        scored = (1 - w_st) * scored + w_st * (standing.goals_for / standing.played)
        conceded = (1 - w_st) * conceded + w_st * (standing.goals_against / standing.played)
        signals.append(f"saison ({standing.played} matchs)")

    # --- 4. régression vers la moyenne : un petit échantillon ne domine pas ---
    effective_n = form.n + season_games
    shrink = effective_n / (effective_n + 5.0)
    attack = 1.0 + shrink * ((scored / league_avg) - 1.0)
    defense = 1.0 + shrink * ((conceded / league_avg) - 1.0)

    return Strength(
        attack=max(0.25, min(2.6, attack)),
        defense=max(0.25, min(2.6, defense)),
        sample=form.n,
        scored_avg=scored,
        conceded_avg=conceded,
        home_sample=len(form.split(True)),
        away_sample=len(form.split(False)),
        signals=signals,
    )


def h2h_tilt(h2h: list[MatchResult], max_shift: float | None = None) -> float:
    """Inclinaison issue des confrontations directes, dans [−1, 1].

    Positif = avantage à l'équipe dont l'historique est fourni (domicile).
    Amortie par le nombre de confrontations : 2 matchs ne pèsent presque rien.
    """
    if not h2h:
        return 0.0
    diffs = [m.scored - m.conceded for m in h2h]
    mean_diff = sum(diffs) / len(diffs)
    shrink = len(h2h) / (len(h2h) + 4.0)
    tilt = max(-1.0, min(1.0, mean_diff / 2.0)) * shrink
    cap = cfg.ENGINE.h2h_weight if max_shift is None else max_shift
    return tilt * cap


# ==========================================================================
# 6. Structures de résultat
# ==========================================================================
@dataclass
class MarketLine:
    """Une ligne de marché prête à afficher (probabilité + comparaison marché)."""

    key: str
    label: str
    prob: float
    market_prob: float | None = None
    odds: float | None = None
    edge: float | None = None
    is_value: bool = False
    note: str = ""

    @property
    def fair_odds(self) -> float | None:
        return round(1.0 / self.prob, 2) if self.prob > 0.001 else None

    @property
    def expected_value(self) -> float | None:
        if self.odds and self.odds > 1.0:
            return self.prob * self.odds - 1.0
        return None


@dataclass
class MainPick:
    """Recommandation centrale du moteur (§3).

    Un seul pronostic mis en avant, avec sa probabilité et son niveau de
    confiance. Les autres marchés restent affichés, mais en second rideau.
    """

    key: str
    label: str                 # ce qui est recommandé, en clair
    family: str                # famille de marché, en clair
    probability: float
    confidence: float          # sur 10
    odds: float | None = None
    edge: float | None = None
    is_value: bool = False

    @property
    def fair_odds(self) -> float | None:
        return round(1.0 / self.probability, 2) if self.probability > 0.01 else None

    @property
    def strength(self) -> str:
        if self.probability >= 0.70:
            return "Fort"
        if self.probability >= 0.55:
            return "Solide"
        if self.probability >= 0.45:
            return "Prudent"
        return "Incertain"


# Familles de marchés candidates au pronostic principal.
# `weight` traduit l'intérêt de la recommandation : une double chance est très
# probable mais peu informative, un vainqueur sec dit vraiment quelque chose.
PICK_FAMILIES: dict[str, tuple[str, float]] = {
    "1x2_": ("Vainqueur", 1.00),
    "ml_": ("Vainqueur (prolongations incluses)", 0.98),
    "spread_": ("Handicap", 0.86),
    "puckline_": ("Handicap", 0.86),
    "hcp_": ("Handicap", 0.86),
    "total_": ("Nombre de buts / points", 0.85),
    "sets_": ("Score en sets", 0.82),
    "btts_": ("Les deux équipes marquent", 0.80),
    "dc_": ("Double chance", 0.75),
}


def _pick_family(key: str) -> tuple[str, float] | None:
    for prefix, family in PICK_FAMILIES.items():
        if key.startswith(prefix):
            return family
    return None


def choose_main_pick(pred: "Prediction") -> MainPick | None:
    """Sélectionne LE pronostic à mettre en avant.

    Un pronostic n'a d'intérêt que s'il dit quelque chose. « Plus de 0,5 but »
    à 97 % est vrai mais inutile : sa cote serait de 1,03. On ne retient donc
    que les marchés dont la probabilité reste dans une fourchette exploitable
    (`PICK_MIN_PROBABILITY` … `PICK_MAX_PROBABILITY`), puis on classe par
    probabilité × intérêt de la famille, avec un bonus au marché « vainqueur »
    (la lecture naturelle d'un match) et aux marchés sous-évalués.

    Si aucun marché n'entre dans la fourchette, on retombe sur le meilleur
    candidat sous le plafond ; l'interface indique alors un signal incertain.
    """
    low, high = cfg.ENGINE.pick_min_probability, cfg.ENGINE.pick_max_probability
    scored: list[tuple[float, MarketLine, str]] = []
    fallback: list[tuple[float, MarketLine, str]] = []

    # Le nul n'est retenu que s'il est réellement l'issue la plus probable.
    # L'écarter systématiquement — ce que faisait le moteur — revenait à
    # annoncer une victoire alors que la distribution désignait le nul :
    # le pronostic principal cessait alors de refléter le modèle.
    issues = pred.outcome_probs or {}
    nul_favori = bool(issues) and max(issues, key=issues.get) == "draw"

    for line in pred.lines:
        family = _pick_family(line.key)
        if family is None:
            continue
        if line.key == "1x2_draw" and not nul_favori:
            continue  # un nul minoritaire ne dit rien d'utile
        family_name, weight = family
        score = line.prob * weight
        if line.key.startswith(("1x2_", "ml_")):
            score += cfg.ENGINE.pick_winner_bonus
        if line.is_value:
            score += 0.05
        if line.prob > high:
            continue  # quasi certain : aucune valeur informative
        (scored if line.prob >= low else fallback).append((score, line, family_name))

    pool = scored or fallback
    if not pool:
        return None

    _score, line, family_name = max(pool, key=lambda item: item[0])
    return MainPick(
        key=line.key,
        label=line.label,
        family=family_name,
        probability=line.prob,
        confidence=pred.confidence.score,
        odds=line.odds,
        edge=line.edge,
        is_value=line.is_value,
    )


@dataclass
class Confidence:
    score: float                      # sur 10
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.score >= 7.5:
            return "Élevée"
        if self.score >= 5.5:
            return "Correcte"
        if self.score >= 3.5:
            return "Modérée"
        return "Faible"


@dataclass
class TeamProfile:
    """Photographie statistique d'une équipe, pour l'affichage (§9).

    Chaque champ vaut `None` quand la donnée n'a pas été fournie par une source
    fiable : l'interface affiche alors « — », jamais une valeur inventée.
    """

    team: str
    matches: int = 0
    form_string: str = ""
    streak: str = ""
    rank: int | None = None
    points_per_game: float | None = None
    scored_avg: float | None = None
    conceded_avg: float | None = None
    scored_home: float | None = None
    conceded_home: float | None = None
    scored_away: float | None = None
    conceded_away: float | None = None
    clean_sheet_rate: float | None = None
    btts_rate: float | None = None
    over_rate: float | None = None
    xg_for: float | None = None
    xg_against: float | None = None
    corners_for: float | None = None
    corners_against: float | None = None
    cards: float | None = None
    possession: float | None = None
    shots: float | None = None
    shots_on_target: float | None = None
    rest_days: float | None = None

    @property
    def available(self) -> list[str]:
        """Noms des indicateurs réellement disponibles."""
        return [
            k
            for k, v in self.__dict__.items()
            if k not in {"team", "matches", "form_string", "streak"} and v is not None
        ]


def build_profile(form: TeamForm | None, standing: Standing | None, over_line: float) -> TeamProfile:
    if form is None:
        return TeamProfile(team="—")
    streak_kind, streak_n = form.streak
    streak = f"{streak_n}{streak_kind}" if streak_n else ""
    return TeamProfile(
        team=form.team,
        matches=form.n,
        form_string=form.form_string,
        streak=streak,
        rank=standing.rank if standing else form.rank,
        points_per_game=standing.points_per_game if standing else None,
        scored_avg=form.scored_avg,
        conceded_avg=form.conceded_avg,
        scored_home=form.scored_avg_split(True),
        conceded_home=form.conceded_avg_split(True),
        scored_away=form.scored_avg_split(False),
        conceded_away=form.conceded_avg_split(False),
        clean_sheet_rate=form.clean_sheet_rate,
        btts_rate=form.btts_rate,
        over_rate=form.over_rate(over_line),
        xg_for=form.xg_for,
        xg_against=form.xg_against,
        corners_for=form.extra_avg("corners_for"),
        corners_against=form.extra_avg("corners_against"),
        cards=form.extra_avg("yellow_cards"),
        possession=form.extra_avg("possession"),
        shots=form.extra_avg("shots"),
        shots_on_target=form.extra_avg("shots_on_target"),
        rest_days=form.rest_days,
    )


@dataclass
class Prediction:
    sport: str
    home: str
    away: str
    n_sims: int
    outcome_probs: dict[str, float]                   # home / draw / away
    market_probs: dict[str, float] | None             # no-vig
    blended_target: dict[str, float] | None
    expected: dict[str, float] = field(default_factory=dict)
    top_scores: list[tuple[str, float]] = field(default_factory=list)
    # Scores compatibles avec le pronostic principal. Même distribution que
    # `top_scores`, lue sous condition de l'issue retenue.
    pick_scores: list[tuple[str, float]] = field(default_factory=list)
    # Contradictions détectées avant affichage. Vide en fonctionnement normal.
    consistency: list[str] = field(default_factory=list)
    score_matrix: np.ndarray | None = None
    lines: list[MarketLine] = field(default_factory=list)
    value_bets: list[MarketLine] = field(default_factory=list)
    confidence: Confidence = field(default_factory=lambda: Confidence(0.0))
    verdict: str = ""
    risk: str = ""
    badges: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    provenances: list[Provenance] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    venue_swapped: bool = False
    news: list = field(default_factory=list)
    competition: Competition | None = None
    profile_home: TeamProfile | None = None
    profile_away: TeamProfile | None = None
    h2h_summary: dict = field(default_factory=dict)
    weather: object | None = None
    odds_movement: dict[str, float] = field(default_factory=dict)
    odds_movement_hours: float | None = None
    bookmaker_count: int = 0
    main_pick: MainPick | None = None
    research: object | None = None   # ResearchReport, pour l'affichage
    # Vrai quand l'ancrage vient des cotes de la saison, faute de cote du match.
    used_market_reference: bool = False
    # Tirages bruts de la simulation. Servent à l'agent pour explorer les
    # scénarios alternatifs et mesurer la stabilité des probabilités. Jamais
    # archivés : ils ne sortent pas de la mémoire vive.
    samples: dict[str, object] | None = None

    # -- accès pratiques pour l'interface -------------------------------
    def line(self, key: str) -> MarketLine | None:
        return next((l for l in self.lines if l.key == key), None)

    def lines_group(self, prefix: str) -> list[MarketLine]:
        return [l for l in self.lines if l.key.startswith(prefix)]

    @property
    def favorite(self) -> str:
        probs = {k: v for k, v in self.outcome_probs.items() if k in {"home", "away"}}
        if not probs:
            return ""
        return self.home if probs.get("home", 0) >= probs.get("away", 0) else self.away

    @property
    def favorite_prob(self) -> float:
        return max(self.outcome_probs.get("home", 0.0), self.outcome_probs.get("away", 0.0))

    def to_record(self) -> dict:
        return {
            "sport": self.sport,
            "competition": self.competition.label if self.competition else None,
            "home": self.home,
            "away": self.away,
            "probs": {k: round(v, 4) for k, v in self.outcome_probs.items()},
            "favorite": self.favorite,
            "main_pick": self.main_pick.label if self.main_pick else None,
            "main_pick_prob": round(self.main_pick.probability, 4) if self.main_pick else None,
            "top_score": self.top_scores[0][0] if self.top_scores else None,
            "confidence": round(self.confidence.score, 1),
            "n_sims": self.n_sims,
        }


# ==========================================================================
# 7. Calibration marché / modèle (§6.4)
# ==========================================================================
def blend_probs(
    market: dict[str, float] | None, model: dict[str, float], weight: float | None = None
) -> dict[str, float]:
    """p_final = w · marché + (1−w) · modèle (w paramétrable, §6.4)."""
    if not market:
        return dict(model)
    w = cfg.ENGINE.market_weight if weight is None else weight
    keys = set(model) | set(market)
    out = {}
    for k in keys:
        m = market.get(k)
        d = model.get(k, 0.0)
        out[k] = w * m + (1 - w) * d if m is not None else d
    total = sum(out.values())
    return {k: v / total for k, v in out.items()} if total > 0 else dict(model)


def calibrate_lambdas(
    lam_home: float,
    lam_away: float,
    target: dict[str, float],
    rho: float | None = None,
    with_draw: bool = True,
) -> tuple[float, float, float]:
    """Ajuste (λ domicile, λ extérieur) pour coller aux probabilités cibles.

    Optimisation Nelder-Mead sur les log-λ (garantit λ > 0). Un terme de
    régularisation empêche le total de buts de dériver loin du modèle initial.
    Renvoie (λ_home, λ_away, erreur résiduelle).
    """
    rho = cfg.ENGINE.dixon_coles_rho if rho is None else rho
    base_total = lam_home + lam_away
    t_home = target.get("home", 0.0)
    t_draw = target.get("draw", 0.0)
    t_away = target.get("away", 0.0)

    def loss(params: np.ndarray) -> float:
        lh, la = float(np.exp(params[0])), float(np.exp(params[1]))
        if not (0.05 < lh < 12 and 0.05 < la < 12):
            return 1e6
        matrix = dixon_coles_matrix(lh, la, rho)
        p_home, p_draw, p_away = matrix_outcome_probs(matrix)
        err = (p_home - t_home) ** 2 + (p_away - t_away) ** 2
        if with_draw:
            err += (p_draw - t_draw) ** 2
        # Régularisation douce sur le volume de buts total.
        err += 0.02 * ((lh + la - base_total) / max(base_total, 1e-6)) ** 2
        return err

    start = np.array([math.log(max(lam_home, 1e-3)), math.log(max(lam_away, 1e-3))])
    best = nelder_mead(loss, start, step=0.12, max_iter=500)
    return float(np.exp(best[0])), float(np.exp(best[1])), float(loss(best))


# ==========================================================================
# 8. Détection de valeur (§6.7)
# ==========================================================================
def attach_market_comparison(
    line: MarketLine, market_odds: float | None, market_prob: float | None
) -> MarketLine:
    if market_odds:
        line.odds = market_odds
    if market_prob is not None:
        line.market_prob = market_prob
        line.edge = line.prob - market_prob
        line.is_value = line.edge >= cfg.ENGINE.value_threshold and line.prob >= 0.10
    return line


# ==========================================================================
# 9. Simulations par sport (§6.5)
# ==========================================================================
def _rng(seed: int | None = None) -> np.random.Generator:
    return np.random.default_rng(cfg.ENGINE.seed if seed is None else seed)


def _goal_expectations(
    bundle: Bundle, sport: str, league_avg: float
) -> tuple[float, float, dict]:
    """λ domicile / λ extérieur à partir des forces et de l'avantage du terrain."""
    home_adv = (
        cfg.ENGINE.home_advantage_football
        if sport == "football"
        else cfg.ENGINE.home_advantage_hockey
    )
    s_home = compute_strength(
        bundle.form_home, league_avg, at_home=True, standing=bundle.standing(bundle.home)
    )
    s_away = compute_strength(
        bundle.form_away, league_avg, at_home=False, standing=bundle.standing(bundle.away)
    )

    diag: dict = {"league_avg": league_avg, "home_adv": home_adv}
    if s_home is None or s_away is None:
        # Pas de forme exploitable : on part de la moyenne de référence et on
        # laisse la calibration marché faire tout le travail.
        diag["strength_available"] = False
        return league_avg * home_adv, league_avg, diag

    diag.update(
        strength_available=True,
        attack_home=round(s_home.attack, 3),
        defense_home=round(s_home.defense, 3),
        attack_away=round(s_away.attack, 3),
        defense_away=round(s_away.defense, 3),
        n_home=s_home.sample,
        n_away=s_away.sample,
        signals_home=s_home.signals,
        signals_away=s_away.signals,
    )

    lam_home = league_avg * s_home.attack * s_away.defense * home_adv
    lam_away = league_avg * s_away.attack * s_home.defense

    # --- confrontations directes (§9) ---
    tilt = h2h_tilt(bundle.h2h)
    if tilt:
        lam_home *= 1.0 + tilt
        lam_away *= 1.0 - tilt
        diag["h2h_tilt"] = round(tilt, 4)
        diag["h2h_matches"] = len(bundle.h2h)

    # --- fatigue / jours de repos (§9) ---
    rest_home = rest_multiplier(bundle.form_home)
    rest_away = rest_multiplier(bundle.form_away)
    if rest_home != 1.0 or rest_away != 1.0:
        lam_home *= rest_home
        lam_away *= rest_away
        diag["rest"] = (round(rest_home, 4), round(rest_away, 4))

    return max(0.15, lam_home), max(0.15, lam_away), diag


def _score_labels(home_goals: np.ndarray, away_goals: np.ndarray, top: int = 5) -> list[tuple[str, float]]:
    n = home_goals.size
    pairs, counts = np.unique(
        np.stack([home_goals, away_goals], axis=1), axis=0, return_counts=True
    )
    order = np.argsort(-counts)[:top]
    return [
        (f"{int(pairs[i][0])}-{int(pairs[i][1])}", float(counts[i] / n)) for i in order
    ]


def scores_matching(
    home_goals: np.ndarray, away_goals: np.ndarray, outcome: str, top: int = 5
) -> list[tuple[str, float]]:
    """Scores les plus probables **parmi ceux qui donnent l'issue demandée**.

    Le score le plus probable dans l'absolu appartient souvent à une autre
    issue que l'issue la plus probable, et ce n'est pas une contradiction :
    une victoire à 51 % se disperse sur 1-0, 2-0, 2-1, 3-1…, tandis qu'un nul
    à 24 % se concentre presque entièrement sur 1-1. Le 1-1 peut donc être le
    score le plus fréquent alors que la victoire reste l'issue la plus
    probable.

    Mathématiquement correct, mais illisible : afficher « victoire de A » à
    côté de « score le plus probable 1-1 » donne l'impression que le moteur
    se contredit. On expose donc aussi le score le plus probable **cohérent
    avec le pronostic affiché**, tiré de la même simulation — aucune vérité
    parallèle n'est introduite, on lit simplement la distribution sous
    condition.

    Les probabilités renvoyées restent **absolues**, non conditionnelles :
    « 2-1 à 9 % » veut bien dire 9 % de tous les scénarios, pas 9 % des
    scénarios de victoire. Renormaliser gonflerait artificiellement des
    chiffres que l'utilisateur compare à ceux des autres marchés.
    """
    diff = home_goals - away_goals
    if outcome == "home":
        masque = diff > 0
    elif outcome == "away":
        masque = diff < 0
    elif outcome == "draw":
        masque = diff == 0
    else:
        return []
    if not masque.any():
        return []
    n = home_goals.size          # dénominateur global : probabilités absolues
    pairs, counts = np.unique(
        np.stack([home_goals[masque], away_goals[masque]], axis=1),
        axis=0, return_counts=True,
    )
    order = np.argsort(-counts)[:top]
    return [
        (f"{int(pairs[i][0])}-{int(pairs[i][1])}", float(counts[i] / n)) for i in order
    ]


def simulate_goal_sport(
    bundle: Bundle, sport: str, n_sims: int | None = None, seed: int | None = None
) -> Prediction:
    """Football & hockey : Poisson bivarié + Dixon-Coles + Monte Carlo."""
    n_sims = n_sims or cfg.ENGINE.n_sims
    rng = _rng(seed)
    ctx = bundle.league_context or {}
    default_avg = (
        cfg.ENGINE.league_avg_goals_football
        if sport == "football"
        else cfg.ENGINE.league_avg_goals_hockey
    )
    league_avg = float(ctx.get("avg_per_team") or default_avg)
    if not 0.4 <= league_avg <= 6.0:
        league_avg = default_avg

    lam_home, lam_away, diag = _goal_expectations(bundle, sport, league_avg)

    # --- probabilités du modèle seul (analytique) ---
    model_matrix = dixon_coles_matrix(lam_home, lam_away)
    m_home, m_draw, m_away = matrix_outcome_probs(model_matrix)
    model_probs = {"home": m_home, "draw": m_draw, "away": m_away}

    # --- probabilités de marché no-vig ---
    market_probs = None
    mapped: dict[str, float] | None = None
    if bundle.odds and bundle.odds.has_h2h:
        mapped = map_h2h_odds(bundle.odds.h2h, bundle.home, bundle.away)
        if mapped:
            market_probs = remove_vig(mapped)

    # --- cible = mélange marché / modèle, puis calibration Nelder-Mead ---
    if market_probs:
        target = blend_probs(market_probs, model_probs)
        diag["anchor"] = "cotes du match"
    elif bundle.market_reference:
        # Ancrage de repli : les cotes de clôture de la saison. Elles décrivent
        # les équipes, pas cette rencontre précise, donc elles pèsent moins que
        # de vraies cotes de match.
        weight = cfg.ENGINE.market_weight * cfg.ENGINE.reference_anchor_ratio
        target = blend_probs(bundle.market_reference, model_probs, weight=weight)
        diag["anchor"] = "repère de marché (cotes de la saison)"
        diag["anchor_weight"] = round(weight, 3)
    else:
        target = dict(model_probs)
        diag["anchor"] = "aucun (statistiques seules)"
    lam_home_c, lam_away_c, residual = calibrate_lambdas(lam_home, lam_away, target)
    diag.update(
        lambda_raw=(round(lam_home, 3), round(lam_away, 3)),
        lambda_calibrated=(round(lam_home_c, 3), round(lam_away_c, 3)),
        calibration_residual=round(residual, 6),
        market_weight=cfg.ENGINE.market_weight,
    )

    matrix = dixon_coles_matrix(lam_home_c, lam_away_c)
    home_goals, away_goals = sample_scores(matrix, n_sims, rng)

    total = home_goals + away_goals
    diff = home_goals - away_goals
    p_home = float(np.mean(diff > 0))
    p_draw = float(np.mean(diff == 0))
    p_away = float(np.mean(diff < 0))

    pred = Prediction(
        sport=sport,
        home=bundle.home,
        away=bundle.away,
        n_sims=n_sims,
        outcome_probs={"home": p_home, "draw": p_draw, "away": p_away},
        market_probs=market_probs,
        blended_target=target,
        expected={
            "goals_home": float(np.mean(home_goals)),
            "goals_away": float(np.mean(away_goals)),
            "goals_total": float(np.mean(total)),
            "lambda_home": lam_home_c,
            "lambda_away": lam_away_c,
        },
        top_scores=_score_labels(home_goals, away_goals),
        score_matrix=matrix,
        diagnostics=diag,
        samples={"home": home_goals, "away": away_goals},
        used_market_reference=bool(not market_probs and bundle.market_reference),
    )

    # ---- 1X2 ----
    labels = {"home": bundle.home, "draw": "Match nul", "away": bundle.away}
    for key in ("home", "draw", "away"):
        line = MarketLine(key=f"1x2_{key}", label=labels[key], prob=pred.outcome_probs[key])
        attach_market_comparison(
            line,
            (mapped or {}).get(key),
            (market_probs or {}).get(key),
        )
        pred.lines.append(line)

    # ---- double chance ----
    pred.lines.append(MarketLine("dc_1x", f"{bundle.home} ou nul", float(np.mean(diff >= 0))))
    pred.lines.append(MarketLine("dc_12", "Pas de match nul", float(np.mean(diff != 0))))
    pred.lines.append(MarketLine("dc_x2", f"{bundle.away} ou nul", float(np.mean(diff <= 0))))

    # ---- Over / Under ----
    market_totals = bundle.odds.totals if bundle.odds else {}
    lines_to_show = list(cfg.TOTALS_LINES.get(sport, []))
    for line_value in sorted(set(lines_to_show) | set(market_totals or {})):
        p_over = float(np.mean(total > line_value))
        book = (market_totals or {}).get(line_value, {})
        novig = remove_vig({k: v for k, v in book.items() if v}) if len(book) >= 2 else None
        over_line = MarketLine(
            key=f"total_over_{line_value}",
            label=f"Plus de {line_value:g}",
            prob=p_over,
        )
        attach_market_comparison(over_line, book.get("Over"), (novig or {}).get("Over"))
        under_line = MarketLine(
            key=f"total_under_{line_value}",
            label=f"Moins de {line_value:g}",
            prob=1.0 - p_over,
        )
        attach_market_comparison(under_line, book.get("Under"), (novig or {}).get("Under"))
        pred.lines.extend([over_line, under_line])

    # ---- BTTS ----
    btts = float(np.mean((home_goals > 0) & (away_goals > 0)))
    pred.lines.append(MarketLine("btts_yes", "Les deux marquent : oui", btts))
    pred.lines.append(MarketLine("btts_no", "Les deux marquent : non", 1.0 - btts))

    # ---- Handicap (spreads du marché + puck line pour le hockey) ----
    handicaps = {abs(l) for l in (bundle.odds.spreads if bundle.odds else {})}
    if sport == "hockey":
        handicaps.add(1.5)
    elif not handicaps:
        handicaps.add(1.5)
    for h in sorted(handicaps):
        if h <= 0:
            continue
        p_home_cover = float(np.mean(diff > h))
        p_away_cover = float(np.mean(-diff > h))
        pred.lines.append(
            MarketLine(f"hcp_home_-{h:g}", f"{bundle.home} −{h:g}", p_home_cover)
        )
        pred.lines.append(
            MarketLine(f"hcp_away_-{h:g}", f"{bundle.away} −{h:g}", p_away_cover)
        )

    # ---- Hockey : prolongation et moneyline (vainqueur toutes périodes) ----
    if sport == "hockey":
        p_reg_tie = p_draw
        share = lam_home_c / max(lam_home_c + lam_away_c, 1e-9)
        p_ot_home = min(0.75, max(0.25, 0.5 + 0.6 * (share - 0.5)))
        ml_home = p_home + p_reg_tie * p_ot_home
        ml_away = p_away + p_reg_tie * (1 - p_ot_home)
        pred.lines.append(MarketLine("ml_home", f"{bundle.home} (avec prolong.)", ml_home))
        pred.lines.append(MarketLine("ml_away", f"{bundle.away} (avec prolong.)", ml_away))
        pred.expected["p_overtime"] = p_reg_tie

        # Puck line ±1.5 calculée sur le score final (prolongation incluse).
        ot_mask = diff == 0
        ot_home_wins = rng.random(n_sims) < p_ot_home
        final_diff = diff.astype(int).copy()
        final_diff[ot_mask & ot_home_wins] = 1
        final_diff[ot_mask & ~ot_home_wins] = -1
        pred.lines.append(
            MarketLine("puckline_home_-1.5", f"{bundle.home} −1,5", float(np.mean(final_diff >= 2)))
        )
        pred.lines.append(
            MarketLine("puckline_away_-1.5", f"{bundle.away} −1,5", float(np.mean(final_diff <= -2)))
        )
        pred.lines.append(
            MarketLine("puckline_home_+1.5", f"{bundle.home} +1,5", float(np.mean(final_diff >= -1)))
        )

    # ---- Corners (football uniquement, jamais inventés) ----
    if sport == "football":
        _attach_corners(pred, bundle)

    return pred


def _attach_corners(pred: Prediction, bundle: Bundle) -> None:
    """Corners attendus : uniquement si une API a vraiment fourni la donnée."""
    values: list[float] = []
    for form in (bundle.form_home, bundle.form_away):
        if form is None:
            continue
        avg_for = form.extra_avg("corners_for")
        avg_against = form.extra_avg("corners_against")
        if avg_for is not None and avg_against is not None:
            values.append(avg_for + avg_against)
        elif form.extra_avg("corners_total") is not None:
            values.append(form.extra_avg("corners_total"))
    if not values:
        pred.unavailable.append("corners")
        return
    expected = sum(values) / len(values)
    pred.expected["corners_total"] = expected
    n_samples = sum(
        1
        for form in (bundle.form_home, bundle.form_away)
        if form and form.extra_avg("corners_for") is not None
    )
    # Nombre de corners ≈ Poisson : on en déduit les probabilités de seuils.
    grid = np.arange(0, 30)
    pmf = poisson_pmf(grid, expected)
    for threshold in (8.5, 9.5, 10.5):
        p_over = float(pmf[grid > threshold].sum())
        note = "échantillon limité" if n_samples < 2 else ""
        pred.lines.append(
            MarketLine(f"corners_over_{threshold}", f"Corners > {threshold:g}", p_over, note=note)
        )


def simulate_basket(
    bundle: Bundle, n_sims: int | None = None, seed: int | None = None
) -> Prediction:
    """Basket : points issus du rythme et des attaques/défenses, loi normale corrélée."""
    n_sims = n_sims or cfg.ENGINE.n_sims
    rng = _rng(seed)
    ctx = bundle.league_context or {}
    league_avg = float(ctx.get("avg_per_team") or cfg.ENGINE.league_avg_points_basket)
    if not 50 <= league_avg <= 140:
        league_avg = cfg.ENGINE.league_avg_points_basket

    fh, fa = bundle.form_home, bundle.form_away
    diag: dict = {"league_avg": league_avg}

    def expected_points(att: TeamForm | None, deff: TeamForm | None, home: bool) -> float:
        off = (att.scored_avg_split(home) or att.scored_avg) if att else None
        dfn = (deff.conceded_avg_split(not home) or deff.conceded_avg) if deff else None
        if off is None and dfn is None:
            return league_avg
        if off is None:
            raw = float(dfn)
        elif dfn is None:
            raw = float(off)
        else:
            # Rythme partagé : attaque de l'un pondérée par la défense de l'autre.
            raw = float(off) * (float(dfn) / league_avg)
        # Régression vers la moyenne : un ou deux matchs ne doivent pas dicter
        # le total (sinon un match de présaison produit un score aberrant).
        n = min(att.n if att else 0, deff.n if deff else 0)
        w = n / (n + 5.0)
        return league_avg + w * (raw - league_avg)

    mean_home = expected_points(fh, fa, home=True) + cfg.ENGINE.home_advantage_basket / 2
    mean_away = expected_points(fa, fh, home=False) - cfg.ENGINE.home_advantage_basket / 2
    diag["points_raw"] = (round(mean_home, 2), round(mean_away, 2))

    # Back-to-back : l'effet du repos est particulièrement documenté en NBA (§9).
    rest_home, rest_away = rest_multiplier(fh), rest_multiplier(fa)
    if rest_home != 1.0 or rest_away != 1.0:
        mean_home *= rest_home
        mean_away *= rest_away
        diag["rest"] = (round(rest_home, 4), round(rest_away, 4))

    # --- marché ---
    market_probs, mapped = None, None
    if bundle.odds and bundle.odds.has_h2h:
        mapped = map_h2h_odds(bundle.odds.h2h, bundle.home, bundle.away)
        if mapped:
            market_probs = remove_vig(mapped)

    sd = cfg.ENGINE.basket_points_sd
    corr = cfg.ENGINE.basket_corr
    sd_diff = math.sqrt(2 * sd**2 * (1 - corr))
    sd_total = math.sqrt(2 * sd**2 * (1 + corr))

    model_margin = mean_home - mean_away
    model_p_home = norm_cdf(model_margin / sd_diff)
    model_probs = {"home": model_p_home, "away": 1 - model_p_home}
    target = blend_probs(market_probs, model_probs)

    # Calibration : on décale l'écart pour atteindre la probabilité cible.
    target_home = min(max(target.get("home", model_p_home), 0.005), 0.995)
    margin = norm_ppf(target_home) * sd_diff
    diag["margin_model"] = round(model_margin, 2)
    diag["margin_calibrated"] = round(margin, 2)

    # Calibration du total sur la ligne du marché quand elle existe.
    total_mean = mean_home + mean_away
    market_line = bundle.odds.main_total_line() if bundle.odds else None
    if market_line is not None:
        book = bundle.odds.totals.get(market_line, {})
        novig = remove_vig({k: v for k, v in book.items() if v}) if len(book) >= 2 else None
        if novig and novig.get("Over"):
            implied_mean = market_line + norm_ppf(novig["Over"]) * sd_total
            total_mean = cfg.ENGINE.market_weight * implied_mean + (
                1 - cfg.ENGINE.market_weight
            ) * total_mean
    diag["total_calibrated"] = round(total_mean, 2)

    mean_h = (total_mean + margin) / 2
    mean_a = (total_mean - margin) / 2

    # --- Monte Carlo (normales corrélées) ---
    cov = np.array([[sd**2, corr * sd**2], [corr * sd**2, sd**2]])
    draws = rng.multivariate_normal([mean_h, mean_a], cov, size=n_sims)
    pts_home = np.rint(draws[:, 0])
    pts_away = np.rint(draws[:, 1])
    # Pas d'égalité au basket : on tranche par une possession supplémentaire.
    ties = pts_home == pts_away
    if ties.any():
        bump = rng.random(ties.sum()) < 0.5
        pts_home[ties] += np.where(bump, 1, 0)
        pts_away[ties] += np.where(bump, 0, 1)

    diff = pts_home - pts_away
    totals = pts_home + pts_away
    p_home = float(np.mean(diff > 0))

    pred = Prediction(
        sport="basket",
        home=bundle.home,
        away=bundle.away,
        n_sims=n_sims,
        outcome_probs={"home": p_home, "away": 1.0 - p_home},
        market_probs=market_probs,
        blended_target=target,
        expected={
            "points_home": float(np.mean(pts_home)),
            "points_away": float(np.mean(pts_away)),
            "points_total": float(np.mean(totals)),
            "margin": float(np.mean(diff)),
            "margin_sd": float(np.std(diff)),
        },
        diagnostics=diag,
        samples={"home": pts_home, "away": pts_away},
    )
    pred.top_scores = [
        (f"{int(round(np.mean(pts_home)))}-{int(round(np.mean(pts_away)))}", float("nan"))
    ]

    for key, label in (("home", bundle.home), ("away", bundle.away)):
        line = MarketLine(f"1x2_{key}", label, pred.outcome_probs[key])
        attach_market_comparison(line, (mapped or {}).get(key), (market_probs or {}).get(key))
        pred.lines.append(line)

    # Totaux : la ligne du marché si dispo, sinon la médiane simulée.
    candidate_lines = sorted(set((bundle.odds.totals if bundle.odds else {}) or {}))
    if not candidate_lines:
        candidate_lines = [round(float(np.median(totals)) * 2) / 2 + 0.5]
    for line_value in candidate_lines:
        book = (bundle.odds.totals if bundle.odds else {}).get(line_value, {})
        novig = remove_vig({k: v for k, v in book.items() if v}) if len(book) >= 2 else None
        p_over = float(np.mean(totals > line_value))
        over = MarketLine(f"total_over_{line_value}", f"Plus de {line_value:g} pts", p_over)
        attach_market_comparison(over, book.get("Over"), (novig or {}).get("Over"))
        under = MarketLine(f"total_under_{line_value}", f"Moins de {line_value:g} pts", 1 - p_over)
        attach_market_comparison(under, book.get("Under"), (novig or {}).get("Under"))
        pred.lines.extend([over, under])

    # Spread : ligne du marché si dispo, sinon l'écart attendu arrondi.
    spread_info = bundle.odds.main_spread() if bundle.odds else None
    spread_line = spread_info[0] if spread_info else -round(float(np.mean(diff)) * 2) / 2
    if spread_line:
        p_cover_home = float(np.mean(diff + spread_line > 0))
        pred.lines.append(
            MarketLine(
                f"spread_home_{spread_line:g}",
                f"{bundle.home} {spread_line:+g}",
                p_cover_home,
            )
        )
        pred.lines.append(
            MarketLine(
                f"spread_away_{-spread_line:g}",
                f"{bundle.away} {-spread_line:+g}",
                1 - p_cover_home,
            )
        )
    return pred


# --------------------------------------------------------------------------
# Tennis : modèle point → jeu → set → match
# --------------------------------------------------------------------------
def game_win_prob(p: float) -> float:
    """Probabilité de gagner son jeu de service à partir de p (point gagné)."""
    p = min(max(p, 1e-6), 1 - 1e-6)
    q = 1 - p
    denom = 1 - 2 * p * q
    deuce = (p * p) / denom if denom > 1e-12 else 1.0
    return p**4 * (1 + 4 * q + 10 * q * q) + 20 * p**3 * q**3 * deuce


def tiebreak_win_prob(p_a: float, p_b: float, target: int = 7) -> float:
    """Probabilité que A gagne le jeu décisif (programmation dynamique exacte).

    À 6-6 le jeu peut durer indéfiniment : l'état est périodique (chaque bloc de
    deux points contient un service de A et un de B), on le résout par la
    formule fermée `xy / (xy + (1−x)(1−y))` plutôt que par récursion.
    """
    from functools import lru_cache

    x = min(max(p_a, 1e-6), 1 - 1e-6)          # A gagne son point de service
    y = min(max(1.0 - p_b, 1e-6), 1 - 1e-6)    # A gagne le point sur le service de B
    denom = x * y + (1 - x) * (1 - y)
    deuce = (x * y) / denom if denom > 1e-12 else 0.5

    @lru_cache(maxsize=None)
    def rec(a: int, b: int) -> float:
        if a >= target and a - b >= 2:
            return 1.0
        if b >= target and b - a >= 2:
            return 0.0
        if a >= target - 1 and b >= target - 1 and a == b:
            return deuce
        # Rotation du service au tennis : 1 point, puis 2 par 2.
        a_serves = ((a + b + 1) // 2) % 2 == 0
        p = x if a_serves else y
        return p * rec(a + 1, b) + (1 - p) * rec(a, b + 1)

    return rec(0, 0)


def set_win_prob(hold_a: float, hold_b: float, p_tb: float) -> float:
    """Probabilité que A gagne un set (DP sur le score en jeux)."""
    from functools import lru_cache

    @lru_cache(maxsize=None)
    def rec(a: int, b: int) -> float:
        if a >= 6 and a - b >= 2:
            return 1.0
        if b >= 6 and b - a >= 2:
            return 0.0
        if a == 6 and b == 6:
            return p_tb
        a_serves = (a + b) % 2 == 0
        p = hold_a if a_serves else 1 - hold_b
        return p * rec(a + 1, b) + (1 - p) * rec(a, b + 1)

    return rec(0, 0)


def match_win_prob_from_set(p_set: float, best_of: int = 3) -> float:
    q = 1 - p_set
    if best_of == 5:
        return p_set**3 * (1 + 3 * q + 6 * q * q)
    return p_set**2 * (1 + 2 * q)


def _tennis_model_prob(bundle: Bundle) -> float | None:
    """Probabilité « modèle » du joueur A à partir de la forme et du classement."""
    fh, fa = bundle.form_home, bundle.form_away
    signals: list[float] = []
    if fh and fa and fh.n and fa.n:
        r_h, r_a = fh.points_rate or 0.5, fa.points_rate or 0.5
        if r_h + r_a > 0:
            signals.append(0.5 + 0.6 * (r_h - r_a) / 2)
    rank_h = (fh.extra.get("rank") if fh else None) or None
    rank_a = (fa.extra.get("rank") if fa else None) or None
    if rank_h and rank_a:
        # Écart de classement transformé en probabilité (échelle logarithmique).
        delta = math.log(rank_a) - math.log(rank_h)
        signals.append(1 / (1 + math.exp(-0.55 * delta)))
    if not signals:
        return None
    return min(0.92, max(0.08, sum(signals) / len(signals)))


def simulate_tennis(
    bundle: Bundle, n_sims: int | None = None, seed: int | None = None, best_of: int = 3
) -> Prediction:
    """Tennis : calibration des probabilités de tenir le service, puis Monte Carlo."""
    n_sims = n_sims or cfg.ENGINE.n_sims
    rng = _rng(seed)
    diag: dict = {"best_of": best_of}

    market_probs, mapped = None, None
    if bundle.odds and bundle.odds.has_h2h:
        mapped = map_h2h_odds(bundle.odds.h2h, bundle.home, bundle.away)
        if mapped:
            market_probs = remove_vig(mapped)

    model_p = _tennis_model_prob(bundle)
    base_serve = float(
        (bundle.form_home.extra.get("serve_pts_won") if bundle.form_home else None) or 0.635
    )
    diag["base_serve"] = base_serve

    if market_probs and model_p is not None:
        target_a = cfg.ENGINE.market_weight * market_probs.get("home", 0.5) + (
            1 - cfg.ENGINE.market_weight
        ) * model_p
    elif market_probs:
        target_a = market_probs.get("home", 0.5)
    elif model_p is not None:
        target_a = model_p
    else:
        target_a = 0.5
    target_a = min(0.97, max(0.03, target_a))
    diag["target_home"] = round(target_a, 4)

    # --- inversion : quel écart de points au service reproduit la cible ? ---
    def match_prob(delta: float) -> float:
        p_a = min(0.92, max(0.35, base_serve + delta))
        p_b = min(0.92, max(0.35, base_serve - delta))
        hold_a, hold_b = game_win_prob(p_a), game_win_prob(p_b)
        p_tb = tiebreak_win_prob(p_a, p_b)
        return match_win_prob_from_set(set_win_prob(hold_a, hold_b, p_tb), best_of)

    lo, hi = -0.22, 0.22
    for _ in range(60):
        mid = (lo + hi) / 2
        if match_prob(mid) < target_a:
            lo = mid
        else:
            hi = mid
    delta = (lo + hi) / 2
    p_a = min(0.92, max(0.35, base_serve + delta))
    p_b = min(0.92, max(0.35, base_serve - delta))
    hold_a, hold_b = game_win_prob(p_a), game_win_prob(p_b)
    p_tb = tiebreak_win_prob(p_a, p_b)
    diag.update(
        serve_pts=(round(p_a, 4), round(p_b, 4)),
        hold=(round(hold_a, 4), round(hold_b, 4)),
        tiebreak=round(p_tb, 4),
    )

    # --- Monte Carlo vectorisé : sets → jeux ---
    sets_needed = 3 if best_of == 5 else 2
    sets_a = np.zeros(n_sims, dtype=np.int16)
    sets_b = np.zeros(n_sims, dtype=np.int16)
    games_total = np.zeros(n_sims, dtype=np.int16)

    for set_idx in range(best_of):
        playing = (sets_a < sets_needed) & (sets_b < sets_needed)
        g_a = np.zeros(n_sims, dtype=np.int16)
        g_b = np.zeros(n_sims, dtype=np.int16)
        done = ~playing
        # Un set ne dépasse jamais 13 jeux (7-6).
        for game_idx in range(13):
            a_serves = (game_idx + set_idx) % 2 == 0
            p_win = hold_a if a_serves else 1 - hold_b
            is_tb = (g_a == 6) & (g_b == 6) & ~done
            wins = rng.random(n_sims) < p_win
            if is_tb.any():
                wins = np.where(is_tb, rng.random(n_sims) < p_tb, wins)
            active = ~done
            g_a += (active & wins).astype(np.int16)
            g_b += (active & ~wins).astype(np.int16)
            done = done | (
                ((g_a >= 6) & (g_a - g_b >= 2))
                | ((g_b >= 6) & (g_b - g_a >= 2))
                | (g_a == 7)
                | (g_b == 7)
            )
            if done.all():
                break
        won_a = (g_a > g_b) & playing
        won_b = (g_b > g_a) & playing
        sets_a += won_a.astype(np.int16)
        sets_b += won_b.astype(np.int16)
        games_total += np.where(playing, g_a + g_b, 0).astype(np.int16)

    p_home = float(np.mean(sets_a > sets_b))

    pred = Prediction(
        sport="tennis",
        home=bundle.home,
        away=bundle.away,
        n_sims=n_sims,
        outcome_probs={"home": p_home, "away": 1 - p_home},
        market_probs=market_probs,
        blended_target={"home": target_a, "away": 1 - target_a},
        expected={
            "games_total": float(np.mean(games_total)),
            "hold_home": hold_a,
            "hold_away": hold_b,
            "sets_home": float(np.mean(sets_a)),
            "sets_away": float(np.mean(sets_b)),
        },
        diagnostics=diag,
        samples={"home": sets_a, "away": sets_b, "games": games_total},
    )

    # Score en sets le plus probable.
    combos: dict[str, float] = {}
    for a, b in zip(sets_a.tolist(), sets_b.tolist()):
        combos[f"{a}-{b}"] = combos.get(f"{a}-{b}", 0.0) + 1.0
    pred.top_scores = sorted(
        ((k, v / n_sims) for k, v in combos.items()), key=lambda kv: -kv[1]
    )[:5]

    for key, label in (("home", bundle.home), ("away", bundle.away)):
        line = MarketLine(f"1x2_{key}", label, pred.outcome_probs[key])
        attach_market_comparison(line, (mapped or {}).get(key), (market_probs or {}).get(key))
        pred.lines.append(line)

    for score, prob in pred.top_scores[:4]:
        pred.lines.append(MarketLine(f"sets_{score}", f"Score en sets {score}", prob))

    market_totals = (bundle.odds.totals if bundle.odds else {}) or {}
    candidate_lines = sorted(set(market_totals) | set(cfg.TOTALS_LINES.get("tennis", [])))
    for line_value in candidate_lines:
        book = market_totals.get(line_value, {})
        novig = remove_vig({k: v for k, v in book.items() if v}) if len(book) >= 2 else None
        p_over = float(np.mean(games_total > line_value))
        over = MarketLine(f"total_over_{line_value}", f"Plus de {line_value:g} jeux", p_over)
        attach_market_comparison(over, book.get("Over"), (novig or {}).get("Over"))
        under = MarketLine(f"total_under_{line_value}", f"Moins de {line_value:g} jeux", 1 - p_over)
        attach_market_comparison(under, book.get("Under"), (novig or {}).get("Under"))
        pred.lines.extend([over, under])

    return pred


# ==========================================================================
# 10. Confiance (§6.9), verdict et scénario imprévu (§6.8)
# ==========================================================================
def compute_confidence(
    bundle: Bundle, pred: Prediction, report: object | None = None
) -> Confidence:
    """Confiance /10 : quantité, profondeur, fraîcheur et recoupement des données."""
    comps: dict[str, float] = {}
    reasons: list[str] = []

    # -- cotes du marché (0-3) --
    if bundle.odds and bundle.odds.has_h2h:
        books = bundle.odds.bookmaker_count
        base = 3.0 if books >= 5 else (2.4 if books >= 2 else 1.6)
        # Un fort désaccord entre bookmakers signale un marché incertain.
        dispersion = bundle.odds.dispersion
        if dispersion is not None and dispersion > 0.10:
            base *= 0.85
            reasons.append("Bookmakers en désaccord")
        comps["marché"] = round(base, 2)
    elif bundle.market_reference:
        # Un repère tiré des cotes de la saison vaut mieux que rien, mais
        # nettement moins qu'une cote publiée pour ce match précis.
        comps["marché"] = 1.3
        reasons.append("Pas de cote du match : repère tiré de la saison")
    else:
        comps["marché"] = 0.0
        reasons.append("Cotes du marché indisponibles")

    # -- profondeur de la forme (0-3) --
    n_home = bundle.form_home.n if bundle.form_home else 0
    n_away = bundle.form_away.n if bundle.form_away else 0
    n_min = min(n_home, n_away)
    comps["forme"] = round(min(3.0, 3.0 * n_min / 8.0), 2)
    if n_min < cfg.ENGINE.min_matches:
        reasons.append("Historique récent trop mince")

    # -- fraîcheur des données (0-2) --
    ages = [p.age_seconds for p in bundle.provenances] or [1e9]
    worst = max(ages)
    if worst < 12 * 3600:
        comps["fraîcheur"] = 2.0
    elif worst < 48 * 3600:
        comps["fraîcheur"] = 1.2
    else:
        comps["fraîcheur"] = 0.4
        reasons.append("Données issues du cache (périmées)")

    # -- accord modèle / marché (0-2) --
    if pred.market_probs:
        gap = max(
            abs(pred.outcome_probs.get(k, 0.0) - pred.market_probs.get(k, 0.0))
            for k in pred.market_probs
        )
        comps["accord"] = round(max(0.0, 2.0 * (1 - gap / 0.25)), 2)
        if gap > 0.15:
            reasons.append("Écart notable entre le modèle et le marché")
    else:
        comps["accord"] = 0.0

    # -- richesse des données (0-1) : classement, H2H, xG --
    richness = 0.0
    if bundle.standings:
        richness += 0.4
    if bundle.h2h:
        richness += 0.3
    if (bundle.form_home and bundle.form_home.xg_for is not None) and (
        bundle.form_away and bundle.form_away.xg_for is not None
    ):
        richness += 0.3
    comps["richesse"] = round(richness, 2)
    if richness == 0.0:
        reasons.append("Ni classement ni statistiques avancées disponibles")

    score = min(10.0, sum(comps.values()))

    # -- qualité de la collecte : le recoupement multi-sources module le tout --
    if report is not None:
        quality = float(getattr(report, "reliability", 0.0) or 0.0)
        # 0.5 de fiabilité laisse le score inchangé ; en dessous il baisse,
        # au-dessus il monte légèrement. L'effet reste borné à ±15 %.
        modifier = max(0.85, min(1.15, 0.85 + 0.6 * quality))
        comps["recoupement"] = round((modifier - 1.0) * score, 2)
        score = min(10.0, score * modifier)
        for issue in getattr(report, "inconsistencies", [])[:2]:
            reasons.append(issue.public_text())

    return Confidence(score=round(score, 1), components=comps, reasons=reasons)


def _pct(x: float) -> str:
    return f"{100 * x:.0f} %"


def _num(x: float, digits: int = 1) -> str:
    """Nombre au format français (virgule décimale)."""
    return f"{x:.{digits}f}".replace(".", ",")


def attach_pick_scores(pred: Prediction) -> None:
    """Score le plus probable **compatible avec le pronostic principal**.

    Ne remplace pas `top_scores`, qui reste la lecture brute de la
    distribution : les deux viennent des mêmes tirages, on en expose
    simplement une seconde lecture, sous condition de l'issue retenue.
    """
    pred.pick_scores = []
    pick = pred.main_pick
    samples = pred.samples or {}
    home, away = samples.get("home"), samples.get("away")
    if pick is None or home is None or away is None:
        return
    issue = {
        "1x2_home": "home", "1x2_draw": "draw", "1x2_away": "away",
        "ml_home": "home", "ml_away": "away",
    }.get(pick.key)
    if issue is None:
        return          # marché sans issue 1X2 : rien à conditionner
    pred.pick_scores = scores_matching(home, away, issue)


def check_consistency(pred: Prediction) -> list[str]:
    """Contradictions entre les sorties affichées, s'il en reste.

    Tous les marchés dérivant des mêmes tirages Monte Carlo, une véritable
    impossibilité ne devrait jamais survenir. Ce contrôle est donc un
    garde-fou : il attrape une régression future — un marché qu'on brancherait
    par erreur sur une autre source — avant que l'utilisateur ne la voie.

    On ne signale que des **impossibilités logiques**, jamais une simple
    tension. Un score modal appartenant à une autre issue que l'issue la plus
    probable n'en est pas une : c'est le comportement normal d'une
    distribution, et le crier serait un faux positif permanent.
    """
    soucis: list[str] = []

    def proba(cle: str) -> float | None:
        ligne = pred.line(cle)
        return None if ligne is None else ligne.prob

    # 1. Les issues 1X2 forment une partition : leur somme vaut 1.
    issues = pred.outcome_probs or {}
    if issues:
        total = sum(issues.values())
        if abs(total - 1.0) > 0.01:
            soucis.append(f"Les issues 1X2 totalisent {total:.1%} au lieu de 100 %")

    # 2. Double chance et issue simple doivent concorder.
    for cle_dc, composantes in (
        ("dc_1x", ("home", "draw")), ("dc_x2", ("draw", "away")),
        ("dc_12", ("home", "away")),
    ):
        p_dc = proba(cle_dc)
        if p_dc is None or not issues:
            continue
        attendu = sum(issues.get(k, 0.0) for k in composantes)
        if abs(p_dc - attendu) > 0.02:
            soucis.append(
                f"{cle_dc} annonce {p_dc:.1%} alors que ses composantes valent {attendu:.1%}"
            )

    # 3. Over et Under d'une même ligne sont complémentaires.
    for ligne in pred.lines:
        if not ligne.key.startswith("total_over_"):
            continue
        p_under = proba(ligne.key.replace("total_over_", "total_under_"))
        if p_under is not None and abs(ligne.prob + p_under - 1.0) > 0.02:
            soucis.append(
                f"{ligne.key} et son Under totalisent {ligne.prob + p_under:.1%}"
            )

    # 4. BTTS oui/non également.
    p_oui, p_non = proba("btts_yes"), proba("btts_no")
    if p_oui is not None and p_non is not None and abs(p_oui + p_non - 1.0) > 0.02:
        soucis.append(f"BTTS oui et non totalisent {p_oui + p_non:.1%}")

    # 5. Le score mis en avant doit vraiment produire l'issue annoncée.
    if pred.main_pick and pred.pick_scores:
        issue = {"1x2_home": "home", "1x2_draw": "draw", "1x2_away": "away",
                 "ml_home": "home", "ml_away": "away"}.get(pred.main_pick.key)
        libelle = pred.pick_scores[0][0]
        try:
            buts_dom, buts_ext = (int(x) for x in libelle.split("-"))
        except ValueError:
            buts_dom = buts_ext = 0
        reel = "home" if buts_dom > buts_ext else ("away" if buts_ext > buts_dom else "draw")
        if issue is not None and reel != issue:
            soucis.append(
                f"Le score {libelle} contredit le pronostic {pred.main_pick.label}"
            )
    return soucis


def build_verdict(pred: Prediction, bundle: Bundle) -> str:
    """Avis court : 2 à 3 phrases maximum (§2)."""
    fav = pred.favorite
    p_fav = pred.favorite_prob
    parts: list[str] = []

    if pred.sport in {"football", "hockey"}:
        draw = pred.outcome_probs.get("draw", 0.0)
        if p_fav < 0.42:
            parts.append(f"Match très ouvert : aucune équipe ne dépasse {_pct(p_fav)}.")
        else:
            parts.append(f"{fav} tient la corde à {_pct(p_fav)}.")
        if pred.top_scores:
            parts.append(
                f"Score le plus probable : {pred.top_scores[0][0]} ({_pct(pred.top_scores[0][1])})."
            )
        total = pred.expected.get("goals_total")
        if total is not None:
            parts.append(f"Volume attendu : {_num(total)} buts, nul à {_pct(draw)}.")
    elif pred.sport == "basket":
        margin = abs(pred.expected.get("margin", 0.0))
        parts.append(f"{fav} favori à {_pct(p_fav)}.")
        parts.append(
            f"Écart attendu de {_num(margin)} points pour un total de "
            f"{pred.expected.get('points_total', 0):.0f}."
        )
    else:  # tennis
        parts.append(f"{fav} favori à {_pct(p_fav)}.")
        if pred.top_scores:
            parts.append(
                f"Scénario le plus probable : {pred.top_scores[0][0]} en sets "
                f"({_pct(pred.top_scores[0][1])})."
            )
        parts.append(f"Total de jeux attendu : {pred.expected.get('games_total', 0):.0f}.")

    if pred.value_bets:
        best = pred.value_bets[0]
        parts.append(f"Écart de valeur repéré sur « {best.label} » (+{_pct(best.edge or 0)}).")

    return " ".join(parts[:3])


def build_risk_line(pred: Prediction, bundle: Bundle) -> str:
    """Une seule ligne : le principal risque qui ferait tomber le pronostic (§6.8)."""
    sport = pred.sport
    p_fav = pred.favorite_prob
    draw = pred.outcome_probs.get("draw", 0.0)

    if bundle.news:
        return "Absence signalée dans l'actualité : à vérifier avant le coup d'envoi."
    if getattr(bundle.weather, "is_rough", False):
        return f"Conditions dégradées annoncées ({bundle.weather.summary()}) : jeu perturbé possible."
    drift = max((abs(v) for v in pred.odds_movement.values()), default=0.0)
    if drift >= 0.08:
        return "Cotes en mouvement marqué avant le match : une information circule."
    if sport == "hockey" and pred.expected.get("p_overtime", 0) > 0.22:
        return "Risque de prolongation élevé : le vainqueur peut basculer sur un tir de barrage."
    if sport in {"football", "hockey"} and draw > 0.28:
        return "Probabilité de nul élevée : le pari vainqueur sec reste fragile."
    if 0.42 <= p_fav <= 0.55:
        return "Match d'équilibre : l'outsider peut renverser la rencontre sur un fait de jeu."
    if sport == "basket" and abs(pred.expected.get("margin", 0)) < 4:
        return "Écart serré : un money-time peut inverser le résultat du handicap."
    if sport == "tennis":
        return "Un service en difficulté ou un abandon change radicalement le scénario."
    if not bundle.odds:
        return "Sans cotes de marché, le pronostic repose uniquement sur la forme récente."
    if p_fav > 0.70:
        return "Favori net : le principal risque est une rotation d'effectif ou une blessure précoce."
    return "Composition officielle non connue : à vérifier avant toute décision."


def collect_value_bets(pred: Prediction) -> list[MarketLine]:
    """Ne retient que les écarts significatifs (§6.7), tri par écart décroissant."""
    values = [l for l in pred.lines if l.is_value and l.odds]
    return sorted(values, key=lambda l: -(l.edge or 0))[:4]


# ==========================================================================
# 11. Point d'entrée : analyse complète d'un match
# ==========================================================================
def analyse(
    bundle: Bundle,
    n_sims: int | None = None,
    seed: int | None = None,
    report: object | None = None,
) -> Prediction:
    """Exécute toute la chaîne d'analyse pour un match donné.

    `report` est le `ResearchReport` produit par la recherche approfondie : il
    module la confiance selon la qualité du recoupement entre sources.
    """
    # Le marché fait foi sur l'identité du terrain : on réaligne si besoin.
    # On compare l'alignement de la PAIRE complète, plus robuste que le seul
    # nom du domicile quand les deux noms se ressemblent.
    if bundle.odds and bundle.odds.home_team and bundle.odds.away_team:
        s_direct = name_similarity(bundle.odds.home_team, bundle.home) + name_similarity(
            bundle.odds.away_team, bundle.away
        )
        s_swap = name_similarity(bundle.odds.home_team, bundle.away) + name_similarity(
            bundle.odds.away_team, bundle.home
        )
        if s_swap > s_direct + 0.05:
            bundle.home, bundle.away = bundle.away, bundle.home
            bundle.form_home, bundle.form_away = bundle.form_away, bundle.form_home
            swapped = True
        else:
            swapped = False
    else:
        swapped = False

    if bundle.sport == "basket":
        pred = simulate_basket(bundle, n_sims=n_sims, seed=seed)
    elif bundle.sport == "tennis":
        pred = simulate_tennis(bundle, n_sims=n_sims, seed=seed)
    else:
        pred = simulate_goal_sport(bundle, bundle.sport, n_sims=n_sims, seed=seed)

    pred.venue_swapped = swapped
    pred.competition = bundle.competition
    pred.provenances = list(bundle.provenances)
    pred.news = list(bundle.news)
    pred.weather = bundle.weather
    if bundle.odds:
        pred.odds_movement = dict(bundle.odds.movement)
        pred.odds_movement_hours = bundle.odds.movement_hours
        pred.bookmaker_count = bundle.odds.bookmaker_count

    # --- profils statistiques enrichis (§9) ---
    over_line = {"football": 2.5, "hockey": 5.5, "basket": 220.5}.get(bundle.sport, 21.5)
    pred.profile_home = build_profile(
        bundle.form_home, bundle.standing(bundle.home), over_line
    )
    pred.profile_away = build_profile(
        bundle.form_away, bundle.standing(bundle.away), over_line
    )
    if bundle.h2h:
        wins = sum(1 for m in bundle.h2h if m.outcome == "W")
        draws = sum(1 for m in bundle.h2h if m.outcome == "D")
        pred.h2h_summary = {
            "n": len(bundle.h2h),
            "home_wins": wins,
            "draws": draws,
            "away_wins": len(bundle.h2h) - wins - draws,
            "avg_goals": sum(m.scored + m.conceded for m in bundle.h2h) / len(bundle.h2h),
            "last": [
                (m.date.strftime("%d/%m/%y"), f"{int(m.scored)}-{int(m.conceded)}")
                for m in bundle.h2h[:5]
            ],
        }

    pred.value_bets = collect_value_bets(pred)
    pred.research = report
    pred.confidence = compute_confidence(bundle, pred, report)
    pred.main_pick = choose_main_pick(pred)
    attach_pick_scores(pred)
    pred.consistency = check_consistency(pred)
    pred.verdict = build_verdict(pred, bundle)
    pred.risk = build_risk_line(pred, bundle)

    # Badges courts affichés dans l'interface, en langage courant.
    if not bundle.odds:
        pred.badges.append("Cotes indisponibles")
        pred.unavailable.append("odds")
    elif bundle.odds.provenance.is_stale:
        pred.badges.append("Cotes enregistrées")
    if bundle.form_home is None or bundle.form_away is None:
        pred.badges.append("Historique incomplet")
        pred.unavailable.append("form")
    if bundle.standings:
        pred.badges.append("Classement pris en compte")
    if bundle.h2h:
        pred.badges.append(f"{len(bundle.h2h)} confrontation(s) directe(s)")
    if pred.profile_home and pred.profile_home.xg_for is not None:
        pred.badges.append("Statistiques avancées")
    if pred.value_bets:
        pred.badges.append("Opportunité repérée")

    return pred
