"""Couche « sources de données » de PronoStat.

Principes :
  * **tout est piloté par compétition** — chaque appel reçoit la `Competition`
    choisie, ce qui restreint les listes d'équipes, réduit les appels API et
    isole le cache (§8) ;
  * chaque API est un client indépendant qui implémente la même interface
    (`BaseProvider`) : ajouter une source (gratuite ou payante) = ajouter une
    classe, sans toucher au moteur ;
  * tout appel réseau passe par `HttpClient` → cache disque + suivi de quota ;
  * aucune donnée n'est inventée : en cas d'échec la fonction renvoie `None`
    et l'interface affiche un badge « données indisponibles ».

Toutes les données remontées portent une `Provenance` (source + horodatage +
cache oui/non) afin d'alimenter le panneau « Sources & fraîcheur ».
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

import requests

import config as cfg
from config import Competition

UTC = timezone.utc
log = logging.getLogger("pronostat.sources")


# ==========================================================================
# Modèles de données
# ==========================================================================
@dataclass
class Provenance:
    """Traçabilité d'une donnée : qui, quand, depuis le cache ou non."""

    source: str
    label: str
    fetched_at: datetime
    from_cache: bool = False
    detail: str = ""
    # Saison décrite par la donnée (année de départ). `None` = la source ne
    # dépend pas d'une saison. Sert à éviter de mélanger deux saisons lors de
    # la fusion : à l'intersaison, les sources ne basculent pas toutes en
    # même temps sur le nouvel exercice.
    season: int | None = None

    @property
    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.fetched_at).total_seconds()

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > cfg.STALE_AFTER

    def freshness(self) -> str:
        age = self.age_seconds
        if age < 90:
            return "à l'instant"
        if age < 3600:
            return f"il y a {int(age // 60)} min"
        if age < 86400:
            return f"il y a {int(age // 3600)} h"
        return f"il y a {int(age // 86400)} j"


@dataclass
class MatchResult:
    """Un match terminé, vu du point de vue d'une équipe donnée.

    `extra` accueille toutes les statistiques enrichies que la source fournit :
    xg_for, xg_against, possession, shots, shots_on_target, corners_for,
    corners_against, yellow_cards, red_cards…
    """

    date: datetime
    opponent: str
    home: bool
    scored: float
    conceded: float
    competition: str = ""
    extra: dict[str, float] = field(default_factory=dict)

    @property
    def outcome(self) -> str:
        if self.scored > self.conceded:
            return "W"
        if self.scored < self.conceded:
            return "L"
        return "D"

    @property
    def clean_sheet(self) -> bool:
        return self.conceded == 0

    @property
    def btts(self) -> bool:
        return self.scored > 0 and self.conceded > 0


@dataclass
class TeamForm:
    """Forme récente et profil statistique d'une équipe / d'un joueur."""

    team: str
    sport: str
    matches: list[MatchResult]
    provenance: Provenance
    extra: dict[str, Any] = field(default_factory=dict)

    # -- agrégats de base ------------------------------------------------
    @property
    def n(self) -> int:
        return len(self.matches)

    def _avg(self, values: Sequence[float]) -> float | None:
        vals = [v for v in values if v is not None]
        return sum(vals) / len(vals) if vals else None

    @property
    def scored_avg(self) -> float | None:
        return self._avg([m.scored for m in self.matches])

    @property
    def conceded_avg(self) -> float | None:
        return self._avg([m.conceded for m in self.matches])

    def split(self, home: bool) -> list[MatchResult]:
        return [m for m in self.matches if m.home is home]

    def scored_avg_split(self, home: bool) -> float | None:
        return self._avg([m.scored for m in self.split(home)])

    def conceded_avg_split(self, home: bool) -> float | None:
        return self._avg([m.conceded for m in self.split(home)])

    @property
    def form_string(self) -> str:
        """Ex. 'WWDLW' — du plus récent au plus ancien."""
        return "".join(m.outcome for m in self.matches[:5])

    @property
    def points_rate(self) -> float | None:
        """Points par match façon championnat (3/1/0), normalisé sur 1."""
        if not self.matches:
            return None
        pts = sum({"W": 3, "D": 1, "L": 0}[m.outcome] for m in self.matches)
        return pts / (3 * len(self.matches))

    def extra_avg(self, key: str) -> float | None:
        """Moyenne d'une statistique enrichie, par match si possible.

        Certaines sources ne publient qu'un **agrégat de saison**, sans le
        détail rencontre par rencontre. Leur valeur est alors déposée dans
        `self.extra` et sert de repli : une moyenne de saison vaut mieux
        qu'un « donnée indisponible », tant qu'on ne la fait pas passer pour
        une moyenne des dix derniers matchs.
        """
        valeurs = [m.extra.get(key) for m in self.matches if key in m.extra]
        if valeurs:
            return self._avg(valeurs)
        return _to_float(self.extra.get(key))

    def extra_count(self, key: str) -> int:
        return sum(1 for m in self.matches if key in m.extra)

    # -- signaux enrichis (§9) ------------------------------------------
    @property
    def clean_sheet_rate(self) -> float | None:
        return (
            sum(1 for m in self.matches if m.clean_sheet) / self.n if self.n else None
        )

    @property
    def btts_rate(self) -> float | None:
        return sum(1 for m in self.matches if m.btts) / self.n if self.n else None

    def over_rate(self, line: float) -> float | None:
        if not self.n:
            return None
        return sum(1 for m in self.matches if m.scored + m.conceded > line) / self.n

    @property
    def streak(self) -> tuple[str, int]:
        """Série en cours, ex. ('W', 3) pour trois victoires de suite."""
        if not self.matches:
            return ("-", 0)
        first = self.matches[0].outcome
        count = 0
        for m in self.matches:
            if m.outcome != first:
                break
            count += 1
        return (first, count)

    @property
    def rest_days(self) -> float | None:
        """Jours écoulés depuis le dernier match (fatigue / back-to-back)."""
        if not self.matches:
            return None
        delta = datetime.now(UTC) - self.matches[0].date
        return max(0.0, delta.total_seconds() / 86400.0)

    @property
    def xg_for(self) -> float | None:
        return self.extra_avg("xg_for")

    @property
    def xg_against(self) -> float | None:
        return self.extra_avg("xg_against")

    @property
    def rank(self) -> int | None:
        value = self.extra.get("rank")
        return int(value) if value else None


@dataclass
class ScorerOdds:
    """Chance qu'un joueur marque, telle que le marché la cote."""

    player: str
    price: float                 # cote décimale « marque à tout moment »
    probability: float           # 1/cote — marge du bookmaker incluse
    bookmaker: str = ""


@dataclass
class ScorerBoard:
    """Tableau des buteurs probables d'un match."""

    scorers: list[ScorerOdds] = field(default_factory=list)
    provenance: "Provenance | None" = None


@dataclass
class Fixture:
    """Rencontre réellement programmée chez les bookmakers."""

    home: str
    away: str
    starts_at: "datetime | None" = None

    @property
    def label(self) -> str:
        quand = self.starts_at.strftime("%d/%m à %Hh%M") if self.starts_at else "date à venir"
        return f"{self.home} — {self.away}  ·  {quand}"


@dataclass
class Standing:
    """Ligne de classement d'une équipe."""

    team: str
    rank: int
    played: int
    points: int
    goals_for: int
    goals_against: int
    won: int = 0
    drawn: int = 0
    lost: int = 0

    @property
    def points_per_game(self) -> float | None:
        return self.points / self.played if self.played else None

    @property
    def goal_difference(self) -> int:
        return self.goals_for - self.goals_against


@dataclass
class OddsSnapshot:
    """Cotes réelles du marché pour un événement (ancrage §6.2)."""

    home_team: str
    away_team: str
    commence_time: datetime | None
    sport_key: str
    provenance: Provenance
    h2h: dict[str, float] = field(default_factory=dict)          # consensus
    totals: dict[float, dict[str, float]] = field(default_factory=dict)
    spreads: dict[float, dict[str, float]] = field(default_factory=dict)
    bookmaker_count: int = 0
    per_book_h2h: dict[str, dict[str, float]] = field(default_factory=dict)
    movement: dict[str, float] = field(default_factory=dict)     # dérive des cotes
    movement_hours: float | None = None
    btts: dict[str, float] = field(default_factory=dict)   # « les deux marquent »

    @property
    def has_h2h(self) -> bool:
        return len(self.h2h) >= 2

    @property
    def dispersion(self) -> float | None:
        """Écart max entre bookmakers sur l'issue principale (désaccord)."""
        if len(self.per_book_h2h) < 2:
            return None
        spreads = []
        for outcome in self.h2h:
            prices = [
                book[outcome] for book in self.per_book_h2h.values() if outcome in book
            ]
            if len(prices) >= 2:
                spreads.append((max(prices) - min(prices)) / max(prices))
        return max(spreads) if spreads else None

    def main_total_line(self) -> float | None:
        """Ligne de total la plus « centrale » proposée par le marché."""
        if not self.totals:
            return None

        def balance(line: float) -> float:
            book = self.totals[line]
            over, under = book.get("Over"), book.get("Under")
            if not over or not under:
                return 99.0
            return abs(over - under)

        return min(self.totals, key=balance)

    def main_spread(self) -> tuple[float, dict[str, float]] | None:
        if not self.spreads:
            return None

        def balance(line: float) -> float:
            book = self.spreads[line]
            vals = list(book.values())
            return abs(vals[0] - vals[1]) if len(vals) == 2 else 99.0

        line = min(self.spreads, key=balance)
        return line, self.spreads[line]


@dataclass
class OddsAttempt:
    """Une tentative de récupération de cotes, et ce qu'elle a donné.

    Sert à répondre précisément à « pourquoi n'y a-t-il pas de cotes ? »
    plutôt que d'afficher un « indisponible » sans explication.
    """

    source: str
    stage: str          # code technique, pour les journaux
    detail: str         # explication en clair
    success: bool = False


# Explication grand public de chaque cause d'échec.
ODDS_REASONS = {
    "no_key": "Aucune clé de cotes configurée",
    "source_disabled": "Source de cotes désactivée",
    "competition_unsupported": "Compétition non couverte par le fournisseur de cotes",
    "no_sport_key": "Compétition introuvable chez le fournisseur de cotes",
    "no_events": "Aucun match programmé publié pour cette compétition",
    "event_not_found": "Rencontre absente du calendrier du fournisseur",
    "quota_exhausted": "Quota de requêtes épuisé",
    "empty_response": "Le fournisseur n'a renvoyé aucune cote",
    "network_error": "Fournisseur de cotes injoignable",
    "no_market": "Aucun marché exploitable publié pour ce match",
    "success": "Cotes récupérées",
}


@dataclass
class OddsDiagnostics:
    """Trace complète de la recherche de cotes pour un match."""

    attempts: list[OddsAttempt] = field(default_factory=list)

    def add(self, source: str, stage: str, detail: str = "", success: bool = False) -> None:
        self.attempts.append(
            OddsAttempt(source, stage, detail or ODDS_REASONS.get(stage, stage), success)
        )

    @property
    def succeeded(self) -> bool:
        return any(a.success for a in self.attempts)

    @property
    def reason(self) -> str:
        """Cause principale de l'absence de cotes, en clair."""
        if self.succeeded:
            return ODDS_REASONS["success"]
        if not self.attempts:
            return "Aucune source de cotes configurée"
        # La tentative la plus avancée est la plus informative.
        order = [
            "no_key", "source_disabled", "competition_unsupported", "no_sport_key",
            "network_error", "quota_exhausted", "no_events", "event_not_found",
            "empty_response", "no_market",
        ]
        ranked = sorted(
            self.attempts,
            key=lambda a: order.index(a.stage) if a.stage in order else -1,
        )
        return ranked[-1].detail

    @property
    def actionable_hint(self) -> str | None:
        """Ce que l'utilisateur peut faire, s'il peut faire quelque chose."""
        if self.succeeded:
            return None      # les cotes sont là : rien à corriger
        stages = {a.stage for a in self.attempts}
        if not self.attempts or "no_key" in stages or "source_disabled" in stages:
            return ("Renseignez `ODDS_API_KEY` dans la configuration pour activer "
                    "les cotes des bookmakers.")
        if "quota_exhausted" in stages:
            return "Le quota mensuel est épuisé : les cotes reviendront au prochain cycle."
        if "event_not_found" in stages or "no_events" in stages:
            return ("Le match n'est pas encore au calendrier des bookmakers "
                    "(cotes publiées généralement quelques jours avant).")
        return None


@dataclass
class NewsFlag:
    """Signal d'actualité (absence/blessure). Affiché seulement, jamais calculé."""

    team: str
    headline: str
    url: str
    published: datetime | None
    provenance: Provenance


@dataclass
class WeatherInfo:
    """Météo au coup d'envoi. Contexte affiché — non injecté dans le modèle."""

    place: str
    temperature_c: float | None
    precipitation_mm: float | None
    wind_kmh: float | None
    provenance: Provenance

    def summary(self) -> str:
        bits = []
        if self.temperature_c is not None:
            bits.append(f"{self.temperature_c:.0f} °C")
        if self.precipitation_mm is not None:
            bits.append(f"{self.precipitation_mm:.1f} mm")
        if self.wind_kmh is not None:
            bits.append(f"vent {self.wind_kmh:.0f} km/h")
        return " · ".join(bits) if bits else "—"

    @property
    def is_rough(self) -> bool:
        return (self.precipitation_mm or 0) >= 2.0 or (self.wind_kmh or 0) >= 35.0


@dataclass
class Bundle:
    """Tout ce que la couche données a pu réunir pour un match."""

    sport: str
    home: str
    away: str
    competition: Competition | None = None
    form_home: TeamForm | None = None
    form_away: TeamForm | None = None
    odds: OddsSnapshot | None = None
    h2h: list[MatchResult] = field(default_factory=list)
    standings: dict[str, Standing] = field(default_factory=dict)
    news: list[NewsFlag] = field(default_factory=list)
    weather: WeatherInfo | None = None
    # Repère tiré des cotes de clôture de la saison. Sert d'ancrage quand
    # aucune cote en direct n'existe — jamais présenté comme une cote du match.
    market_reference: dict[str, float] | None = None
    market_reference_detail: str = ""
    league_context: dict[str, Any] = field(default_factory=dict)
    provenances: list[Provenance] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Pourquoi les cotes sont là — ou pourquoi elles n'y sont pas.
    odds_diagnostics: "OddsDiagnostics" = field(default_factory=lambda: OddsDiagnostics())

    def track(self, prov: Provenance | None) -> None:
        if prov is not None:
            self.provenances.append(prov)

    def standing(self, team: str) -> Standing | None:
        if not self.standings:
            return None
        match = best_match(team, self.standings.keys(), threshold=0.7)
        return self.standings.get(match) if match else None


# ==========================================================================
# Utilitaires de normalisation de noms (les sources n'écrivent pas pareil)
# ==========================================================================
_NOISE = (
    "fc", "cf", "sc", "ac", "afc", "cd", "ud", "sv", "ss", "as", "rc", "cs",
    "club", "de", "the", "calcio", "bk", "if", "hc",
)


def normalize_name(name: str) -> str:
    txt = unicodedata.normalize("NFKD", name or "")
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = txt.lower()
    # Les points des sigles disparaissent sans couper le mot : « A.S. Roma » → « as roma ».
    txt = txt.replace(".", "")
    txt = re.sub(r"[^a-z0-9 ]+", " ", txt)
    tokens = [t for t in txt.split() if t and t not in _NOISE]
    return " ".join(tokens) if tokens else txt.strip()


# Part minimale de mots communs pour accorder la prime de ressemblance.
# En dessous, un seul mot partagé ne prouve rien : « Coventry City » et
# « Manchester City » partagent « city » sans être le même club — de même
# pour « Manchester United » et « Manchester City ». Cette confusion faisait
# analyser une équipe avec les résultats d'une autre.
_TOKEN_OVERLAP_MIN = 0.5


def _ratio(a: str, b: str) -> float:
    """Ressemblance littérale, garantie symétrique.

    `SequenceMatcher` ne renvoie pas exactement la même valeur selon l'ordre
    des arguments. Sans ordre canonique, une même paire d'équipes obtenait
    deux scores différents selon laquelle était à domicile.
    """
    first, second = sorted((a, b))
    return SequenceMatcher(None, first, second).ratio()


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.94
    ta, tb = set(na.split()), set(nb.split())
    ratio = _ratio(na, nb)
    only_a, only_b = ta - tb, tb - ta

    # Chaque nom porte un mot que l'autre n'a pas : ce sont deux entités
    # distinctes, sauf si ces mots sont de simples variantes d'écriture
    # (« Munchen » / « Munich »). Sans ce garde-fou, la seule ressemblance
    # littérale suffisait à confondre « Manchester United » et
    # « Manchester City ».
    if only_a and only_b:
        closest = max(_ratio(wa, wb) for wa in only_a for wb in only_b)
        if closest < 0.70:
            return min(ratio, 0.60)

    if ta & tb:
        jaccard = len(ta & tb) / len(ta | tb)
        if jaccard >= _TOKEN_OVERLAP_MIN:
            return max(0.75 + 0.2 * jaccard, ratio)
    return ratio


def best_match(target: str, candidates: Iterable[str], threshold: float = 0.72) -> str | None:
    best, score = None, 0.0
    for cand in candidates:
        s = name_similarity(target, cand)
        if s > score:
            best, score = cand, s
    return best if score >= threshold else None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    txt = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.fromisoformat(txt) if fmt is None else datetime.strptime(txt, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            continue
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


# `european_season` vit dans config.py : une seule définition pour tout le
# projet (`cfg.european_season()`).


# ==========================================================================
# Cache disque, organisé par compétition (§8)
# ==========================================================================
class CacheStore:
    """Cache clé→valeur persistant dans un unique fichier JSON.

    Chaque entrée porte un `scope` (= compétition) : on peut vider le cache
    d'une compétition sans toucher aux autres.
    """

    def __init__(self, path=None, max_entries: int = 4000):
        # Le chemin est résolu à l'appel (et non à l'import) pour rester
        # surchargeable en test et par configuration.
        self.path = str(path or cfg.HTTP_CACHE_FILE)
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _flush(self) -> None:
        if len(self._data) > self.max_entries:  # purge des plus anciennes
            ordered = sorted(self._data.items(), key=lambda kv: kv[1].get("ts", 0))
            self._data = dict(ordered[-self.max_entries :])
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def make_key(*parts: Any) -> str:
        raw = "|".join(json.dumps(p, sort_keys=True, default=str) for p in parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get(self, key: str, ttl: int) -> tuple[Any, float] | None:
        """Renvoie (valeur, timestamp) si l'entrée existe et respecte le TTL."""
        with self._lock:
            entry = self._data.get(key)
        if not entry:
            return None
        if ttl >= 0 and time.time() - entry.get("ts", 0) > ttl:
            return None
        return entry.get("data"), entry.get("ts", 0)

    def get_stale(self, key: str) -> tuple[Any, float] | None:
        """Version « quota épuisé » : accepte une donnée périmée."""
        return self.get(key, ttl=-1)

    def set(self, key: str, value: Any, scope: str = "global") -> None:
        with self._lock:
            self._data[key] = {"ts": time.time(), "data": value, "scope": scope}
            self._flush()

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._flush()

    def clear_scope(self, scope: str) -> int:
        """Vide le cache d'une seule compétition. Renvoie le nombre d'entrées."""
        with self._lock:
            doomed = [k for k, v in self._data.items() if v.get("scope") == scope]
            for k in doomed:
                self._data.pop(k, None)
            self._flush()
            return len(doomed)

    def scopes(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for entry in self._data.values():
                scope = entry.get("scope", "global")
                out[scope] = out.get(scope, 0) + 1
            return out


# ==========================================================================
# Suivi de quota (§9 du cahier initial)
# ==========================================================================
@dataclass
class QuotaStatus:
    provider: str
    label: str
    used: int
    limit: int
    period: str
    authoritative: bool = False  # True si la valeur vient des en-têtes de l'API

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def ratio(self) -> float:
        return min(1.0, self.used / self.limit) if self.limit else 0.0

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def warning(self) -> bool:
        return self.ratio >= cfg.QUOTA_WARN_RATIO


class QuotaTracker:
    """Compte les appels par fournisseur et par période, persisté sur disque."""

    def __init__(self, path=None):
        self.path = str(path or cfg.QUOTA_FILE)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _flush(self) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def _bucket(period: str, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        return now.strftime("%Y-%m") if period == "month" else now.strftime("%Y-%m-%d")

    def _rule(self, provider: str) -> cfg.QuotaRule | None:
        for rule in cfg.QUOTAS:
            if rule.provider == provider:
                return rule
        return None

    def record(self, provider: str, cost: int = 1) -> None:
        rule = self._rule(provider)
        if not rule:
            return
        key = self._bucket(rule.period)
        with self._lock:
            entry = self._data.setdefault(provider, {})
            if entry.get("bucket") != key:
                entry.update({"bucket": key, "used": 0})
            entry["used"] = int(entry.get("used", 0)) + cost
            entry.pop("authoritative", None)
            self._flush()

    def set_authoritative(self, provider: str, used: int, remaining: int) -> None:
        """Utilise les valeurs exactes renvoyées par l'API (en-têtes HTTP)."""
        rule = self._rule(provider)
        if not rule:
            return
        with self._lock:
            self._data[provider] = {
                "bucket": self._bucket(rule.period),
                "used": int(used),
                "limit": int(used) + int(remaining),
                "authoritative": True,
            }
            self._flush()

    def status(self, provider: str) -> QuotaStatus | None:
        rule = self._rule(provider)
        if not rule:
            return None
        entry = self._data.get(provider, {})
        used = int(entry.get("used", 0)) if entry.get("bucket") == self._bucket(rule.period) else 0
        limit = int(entry.get("limit", rule.limit)) or rule.limit
        return QuotaStatus(
            provider=provider,
            label=rule.label,
            used=used,
            limit=limit,
            period=rule.period,
            authoritative=bool(entry.get("authoritative")),
        )

    def all_status(self) -> list[QuotaStatus]:
        out = []
        for rule in cfg.QUOTAS:
            st = self.status(rule.provider)
            if st:
                out.append(st)
        return out

    def can_spend(self, provider: str, cost: int = 1) -> bool:
        st = self.status(provider)
        return True if st is None else st.remaining >= cost


def consensus_price(prices: "list[float]") -> float:
    """Cote de consensus, résistante aux bookmakers isolés.

    Une moyenne brute laisse un seul opérateur atypique déplacer le
    consensus : sur dix bookmakers à 2,00 et un à 5,00, la moyenne monte à
    2,27 alors que le marché dit clairement 2,00. Comme cette cote sert
    ensuite d'ancrage à toute la méthode no-vig, l'erreur se propage à
    chaque probabilité affichée.

    On procède donc en deux temps :

    1. **La médiane** donne le centre du marché. Elle ne bouge pas si un
       opérateur s'écarte, quelle que soit l'ampleur de son écart.
    2. **On ne retient que les cotes proches de ce centre**, à une tolérance
       relative près — relative, car un écart de 0,10 est énorme sur une cote
       à 1,20 et négligeable sur une cote à 12,00. La moyenne de ces cotes-là
       est plus fine que la médiane seule, tout en restant insensible aux
       valeurs aberrantes.

    Si l'écartement laisse trop peu de cotes — marché très dispersé, ou deux
    opérateurs seulement — on s'en tient à la médiane, qui reste défendable.
    """
    valeurs = sorted(p for p in prices if p and p > 1.0)
    if not valeurs:
        return 0.0
    if len(valeurs) <= 2:
        return round(sum(valeurs) / len(valeurs), 4)

    milieu = len(valeurs) // 2
    mediane = (
        valeurs[milieu] if len(valeurs) % 2
        else (valeurs[milieu - 1] + valeurs[milieu]) / 2
    )
    tolerance = cfg.ODDS_CONSENSUS_TOLERANCE
    proches = [p for p in valeurs if abs(p - mediane) <= mediane * tolerance]
    # Moins de la moitié des opérateurs d'accord : le marché est trop disperse
    # pour qu'une moyenne ait un sens, la médiane fait mieux le travail.
    if len(proches) * 2 < len(valeurs):
        return round(mediane, 4)
    return round(sum(proches) / len(proches), 4)


def _api_error_message(resp: "requests.Response", limite: int = 160) -> str:
    """Extrait l'explication d'une réponse en erreur, si elle en porte une.

    Les API renvoient leur diagnostic sous des clés variables (`message`,
    `error`, `detail`…). On tente le JSON ; une page d'erreur HTML n'apprend
    rien et ne doit surtout pas remonter jusqu'à l'interface, on la laisse de
    côté. Le résultat est toujours tronqué : il finit sous les yeux de
    l'utilisateur.
    """
    try:
        corps = resp.json()
    except ValueError:
        texte = " ".join(resp.text.split())
        if texte[:1] == "<" or "text/html" in resp.headers.get("content-type", ""):
            return ""          # page d'erreur générique : aucun contenu utile
        return texte[:limite]
    if isinstance(corps, dict):
        for cle in ("message", "error", "detail", "error_message", "msg"):
            valeur = corps.get(cle)
            if isinstance(valeur, str) and valeur.strip():
                return valeur.strip()[:limite]
    return " ".join(str(corps).split())[:limite]


# ==========================================================================
# Client HTTP : cache + quota + tolérance aux pannes
# ==========================================================================
class HttpClient:
    """Client HTTP partagé, sûr en usage concurrent.

    Chaque thread reçoit sa propre `requests.Session` (les sessions ne sont pas
    garanties sûres entre threads), tandis que le cache et le compteur de quota
    sont protégés par leurs propres verrous.
    """

    def __init__(self, cache: CacheStore, quota: QuotaTracker):
        self.cache = cache
        self.quota = quota
        self._local = threading.local()
        self.last_error: str | None = None

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"User-Agent": cfg.USER_AGENT})
            self._local.session = session
        return session

    def get_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        ttl: int = 3600,
        provider: str | None = None,
        cost: int = 1,
        expect: str = "json",
        scope: str = "global",
        timeout: float | None = None,
    ) -> tuple[Any, float, bool] | None:
        """Renvoie (données, timestamp, from_cache) ou None.

        Ordre : cache frais → appel réseau (si quota) → cache périmé.
        `scope` sert à ranger l'entrée dans le cache de sa compétition.
        """
        key = CacheStore.make_key(url, params, expect)
        cached = self.cache.get(key, ttl)
        if cached is not None:
            return cached[0], cached[1], True

        if provider and not self.quota.can_spend(provider, cost):
            stale = self.cache.get_stale(key)
            if stale is not None:
                return stale[0], stale[1], True
            self.last_error = f"quota_exhausted:{provider}"
            return None

        # Une coupure réseau passagère ne doit pas coûter les cotes du match.
        # Sans reprise, un seul ConnectionError suffisait à faire chuter la
        # confiance de l'analyse — constaté en conditions réelles. Les essais
        # supplémentaires ne consomment aucun crédit : une requête qui n'a pas
        # abouti n'est pas comptée par le fournisseur.
        resp = None
        for essai in range(cfg.HTTP_RETRIES + 1):
            try:
                resp = self.session.get(
                    url, params=params, headers=headers,
                    timeout=timeout or cfg.HTTP_TIMEOUT,
                )
                break
            except requests.RequestException as exc:
                self.last_error = f"network:{type(exc).__name__}"
                if essai == cfg.HTTP_RETRIES:
                    stale = self.cache.get_stale(key)
                    return (stale[0], stale[1], True) if stale else None
                # Attente croissante : une panne brève se résorbe souvent en
                # moins d'une seconde, inutile de patienter davantage.
                time.sleep(cfg.HTTP_RETRY_DELAY * (essai + 1))
        if resp is None:  # pragma: no cover - garde-fou, la boucle sort avant
            return None

        if provider:
            self._sync_quota(provider, resp, cost)

        if resp.status_code != 200:
            # Le corps porte souvent la seule explication utilisable (marché
            # invalide, clé révoquée, paramètre inconnu). Sans lui, un 422
            # parfaitement explicite se réduit à « aucune réponse » et la
            # panne devient indiagnosticable.
            self.last_error = f"http_{resp.status_code}"
            detail = _api_error_message(resp)
            if detail:
                self.last_error += f": {detail}"
            # 400/401/403/422 traduisent une erreur de configuration de notre
            # côté : clé invalide, marché inexistant, paramètre refusé. Elles
            # méritent d'être vues même en production, où seuls les WARNING
            # sont conservés. Un 404 ou un 5xx sur une source de repli est en
            # revanche un fonctionnement normal : on n'en fait pas une alerte.
            # 429 = débit dépassé. Ce n'est pas une panne du service mais un
            # signal d'usage : il mérite d'être vu, sous peine de chercher
            # longtemps pourquoi une source « ne répond plus ».
            grave = resp.status_code in (400, 401, 403, 422, 429)
            (log.warning if grave else log.info)(
                "%s a repondu %s — %s",
                provider or "source inconnue", resp.status_code, detail or "sans detail",
            )
            stale = self.cache.get_stale(key)
            return (stale[0], stale[1], True) if stale else None

        try:
            payload = resp.text if expect == "text" else resp.json()
        except ValueError:
            self.last_error = "bad_json"
            return None

        self.cache.set(key, payload, scope=scope)
        self.last_error = None
        return payload, time.time(), False

    def _sync_quota(self, provider: str, resp: requests.Response, cost: int) -> None:
        """Les en-têtes de quota font foi quand l'API en fournit."""
        head = {k.lower(): v for k, v in resp.headers.items()}
        used = head.get("x-requests-used") or head.get("x-ratelimit-requests-used")
        remaining = head.get("x-requests-remaining") or head.get(
            "x-ratelimit-requests-remaining"
        )
        if used is not None and remaining is not None:
            try:
                self.quota.set_authoritative(provider, int(float(used)), int(float(remaining)))
                return
            except (TypeError, ValueError):
                pass
        self.quota.record(provider, cost)


# ==========================================================================
# Interface commune des sources (§10 : architecture évolutive)
# ==========================================================================
class BaseProvider:
    """Contrat commun. Toutes les méthodes reçoivent la `Competition` choisie.

    Une source qui ne gère pas une compétition renvoie `None` : l'agrégateur
    passe automatiquement à la suivante.
    """

    name: str = "base"
    label: str = "Base"
    supports: frozenset[str] = frozenset()
    enabled: bool = True
    provides_odds: bool = False

    def __init__(self, http: HttpClient):
        self.http = http

    # -- capacités (toutes optionnelles) --------------------------------
    def handles(self, comp: Competition) -> bool:
        """La source connaît-elle cette compétition précise ?"""
        return comp.sport in self.supports

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        return None

    def form(self, comp: Competition, team: str) -> TeamForm | None:
        return None

    def standings(self, comp: Competition) -> tuple[dict[str, Standing], Provenance] | None:
        return None

    def head_to_head(
        self, comp: Competition, home: str, away: str
    ) -> tuple[list[MatchResult], Provenance] | None:
        return None

    def odds(self, comp: Competition, home: str, away: str) -> OddsSnapshot | None:
        return None

    def news(self, comp: Competition, team: str) -> list[NewsFlag]:
        return []

    # -- helpers --------------------------------------------------------
    def _prov(
        self, ts: float, from_cache: bool, detail: str = "", season: int | None = None
    ) -> Provenance:
        return Provenance(
            source=self.name,
            label=self.label,
            fetched_at=datetime.fromtimestamp(ts, UTC),
            from_cache=from_cache,
            detail=detail,
            season=season,
        )


# --------------------------------------------------------------------------
# 1. The Odds API — ancrage du marché
# --------------------------------------------------------------------------
class TheOddsApiProvider(BaseProvider):
    """Cotes réelles des bookmakers. ~500 requêtes/mois en gratuit.

    Note quota : les endpoints `/v4/sports` et `/v4/sports/{key}/events`
    ne consomment pas de crédit ; seul `/odds` en consomme
    (coût = nb de marchés × nb de régions).
    """

    name = "the_odds_api"
    label = "Cotes des bookmakers"
    supports = frozenset({"football", "basket", "tennis", "hockey"})
    provides_odds = True
    BASE = "https://api.the-odds-api.com/v4"

    def __init__(self, http: HttpClient, api_key: str = "", history: "OddsHistory | None" = None):
        super().__init__(http)
        self.api_key = api_key
        self.enabled = bool(api_key) and cfg.SOURCES.the_odds_api
        self.history = history

    # -- catalogue vivant des compétitions -------------------------------
    def catalogue(self) -> dict[str, dict]:
        """{clé: {title, group, active}} tel que l'API le déclare aujourd'hui."""
        if not self.enabled:
            return {}
        res = self.http.get_json(
            f"{self.BASE}/sports",
            params={"apiKey": self.api_key, "all": "false"},
            ttl=cfg.TTL.catalog,
        )
        if not res or not isinstance(res[0], list):
            return {}
        return {
            s["key"]: {
                "title": s.get("title", ""),
                "group": s.get("group", ""),
                "active": bool(s.get("active")),
            }
            for s in res[0]
            if isinstance(s, dict) and s.get("key")
        }

    def resolve_keys(self, comp: Competition) -> list[str]:
        """Clés The Odds API correspondant à la compétition demandée.

        - clé explicite si elle est encore active ;
        - sinon repli par titre (les clés changent parfois) ;
        - motifs regex pour le tennis, dont les tournois tournent chaque semaine.
        """
        catalogue = self.catalogue()
        if not catalogue:
            return [comp.odds_key] if comp.odds_key else []

        if comp.odds_patterns:
            keys = []
            for key, meta in catalogue.items():
                if not meta["active"]:
                    continue
                blob = f"{key} {meta['title']}".lower()
                if re.search(cfg.TENNIS_EXCLUDED_PATTERN, blob):
                    continue  # §5 : jamais d'ITF / Challenger / Futures
                if any(re.search(p, key) for p in comp.odds_patterns):
                    keys.append(key)
            return sorted(keys)

        if comp.odds_key and comp.odds_key in catalogue:
            return [comp.odds_key]

        if comp.odds_title:
            match = best_match(
                comp.odds_title,
                [m["title"] for m in catalogue.values() if m["active"]],
                threshold=0.80,
            )
            if match:
                return [k for k, m in catalogue.items() if m["title"] == match]
        return []

    def handles(self, comp: Competition) -> bool:
        return self.enabled and comp.sport in self.supports

    def _events(self, sport_key: str, scope: str) -> tuple[list[dict], float, bool] | None:
        res = self.http.get_json(
            f"{self.BASE}/sports/{sport_key}/events",
            params={"apiKey": self.api_key, "dateFormat": "iso"},
            ttl=cfg.TTL.events,
            scope=scope,
        )
        if not res or not isinstance(res[0], list):
            return None
        return res[0], res[1], res[2]

    def fixtures(self, comp: Competition) -> list[Fixture]:
        """Rencontres réellement programmées, telles que les bookmakers les publient.

        Distinction capitale pour l'utilisateur : une compétition compte vingt
        équipes, donc 380 appariements possibles, mais une dizaine seulement
        sont au calendrier à un instant donné. Choisir librement deux équipes
        produit presque toujours une affiche qui n'existe pas — et donc aucune
        cote, sans que rien ne l'ait laissé prévoir.

        L'endpoint `/events` ne consomme aucun crédit : cette liste est
        gratuite, on peut l'afficher systématiquement.
        """
        if not self.handles(comp):
            return []
        sorties: list[Fixture] = []
        for key in self.resolve_keys(comp):
            got = self._events(key, comp.scope)
            if not got:
                continue
            for ev in got[0]:
                domicile, exterieur = ev.get("home_team"), ev.get("away_team")
                if not domicile or not exterieur:
                    continue
                sorties.append(
                    Fixture(
                        home=str(domicile).strip(),
                        away=str(exterieur).strip(),
                        starts_at=_parse_dt(ev.get("commence_time")),
                    )
                )
        # Le prochain match d'abord ; une date absente ne doit pas faire échouer
        # le tri, on la repousse en fin de liste.
        return sorted(
            sorties,
            key=lambda f: (f.starts_at is None, f.starts_at or datetime.max.replace(tzinfo=UTC)),
        )

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        names: set[str] = set()
        newest, cached_all = 0.0, True
        for key in self.resolve_keys(comp):
            got = self._events(key, comp.scope)
            if not got:
                continue
            events, ts, from_cache = got
            newest = max(newest, ts)
            cached_all = cached_all and from_cache
            for ev in events:
                for side in ("home_team", "away_team"):
                    if ev.get(side):
                        names.add(str(ev[side]).strip())
        if not names:
            return None
        return sorted(names), self._prov(
            newest or time.time(), cached_all, f"{comp.label} · matchs à venir"
        )

    # Marchés demandés par sport. Chaque marché coûte un crédit : on ne
    # demande que ceux que le moteur sait exploiter pour ce sport.
    #
    # `btts` est volontairement absent : l'endpoint /odds le refuse avec un
    # HTTP 422 « Markets not supported by this endpoint », ce qui faisait
    # échouer TOUTES les requêtes de cotes football, marchés valides compris.
    # La probabilité « les deux marquent » reste produite par le Monte Carlo
    # (engine.py), elle n'a jamais dépendu du marché.
    MARKETS = {
        "football": "h2h,totals,spreads",
        "hockey": "h2h,totals,spreads",
        "basket": "h2h,totals,spreads",
        "tennis": "h2h,totals",
    }

    # Marché des buteurs. Absent de MARKETS car l'endpoint groupé /odds le
    # refuse : il n'existe que sur l'endpoint d'un événement précis.
    SCORER_MARKET = "player_goal_scorer_anytime"
    # Au-delà, les bookmakers n'ont pas encore ouvert le marché : on n'appelle
    # pas l'API pour rien.
    SCORER_MAX_DAYS = 4

    def goal_scorers(self, comp: Competition, home: str, away: str) -> ScorerBoard | None:
        """Probabilités de marquer, telles que les bookmakers les cotent.

        Deux limites assumées, et affichées à l'utilisateur :

        1. Les bookmakers ne publient ces cotes qu'à l'approche du coup
           d'envoi — souvent moins de trois jours avant. Plus tôt, la réponse
           est vide, et ce n'est pas une panne.
        2. Seul le côté « Yes » est coté. Sans le « No » correspondant, la
           marge du bookmaker ne peut pas être retirée : `1/cote` est donc une
           probabilité **implicite**, légèrement surestimée. On ne prétend pas
           l'avoir corrigée, et on ne normalise pas non plus — « marquer » n'est
           pas un ensemble d'issues exclusives, plusieurs joueurs marquent
           souvent dans le même match.
        """
        if not self.enabled or comp.sport != "football":
            return None
        for key in self.resolve_keys(comp):
            got = self._events(key, comp.scope)
            if not got:
                continue
            event, _score = self._find_event(got[0], home, away)
            if event is None:
                continue
            # Économie de quota : ce marché n'est jamais publié longtemps à
            # l'avance. Interroger un match lointain dépense un crédit pour
            # une réponse vide garantie.
            debut = _parse_dt(event.get("commence_time"))
            if debut is not None and (debut - datetime.now(UTC)).days > self.SCORER_MAX_DAYS:
                log.info(
                    "buteurs %s vs %s : match dans plus de %d jours, marché pas encore ouvert",
                    home, away, self.SCORER_MAX_DAYS,
                )
                return None
            res = self.http.get_json(
                f"{self.BASE}/sports/{key}/events/{event.get('id', '')}/odds",
                params={
                    "apiKey": self.api_key,
                    "regions": os.getenv("ODDS_REGIONS", "eu,uk,us").split(",")[0].strip() or "eu",
                    "markets": self.SCORER_MARKET,
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                },
                ttl=cfg.TTL.odds,
                provider=self.name,
                cost=1,
                scope=comp.scope,
            )
            if not res:
                log.info("buteurs %s vs %s : %s", home, away, self.http.last_error or "réponse vide")
                return None

            payload, ts, from_cache = res
            # Un joueur peut être coté par plusieurs bookmakers : on retient la
            # cote la plus généreuse, celle qui minore le moins ses chances.
            meilleur: dict[str, tuple[float, str]] = {}
            for book in (payload or {}).get("bookmakers", []) or []:
                for marche in book.get("markets", []) or []:
                    if marche.get("key") != self.SCORER_MARKET:
                        continue
                    for issue in marche.get("outcomes", []) or []:
                        if str(issue.get("name", "")).lower() not in ("yes", "oui"):
                            continue
                        joueur = str(issue.get("description") or "").strip()
                        prix = _to_float(issue.get("price"))
                        if not joueur or not prix or prix <= 1.0:
                            continue
                        connu = meilleur.get(joueur)
                        if connu is None or prix > connu[0]:
                            meilleur[joueur] = (prix, str(book.get("title") or book.get("key") or ""))
            if not meilleur:
                return None

            lignes = sorted(
                (
                    ScorerOdds(player=nom, price=prix, probability=1.0 / prix, bookmaker=source)
                    for nom, (prix, source) in meilleur.items()
                ),
                key=lambda s: -s.probability,
            )
            return ScorerBoard(
                scorers=lignes,
                provenance=self._prov(ts, from_cache, f"{len(lignes)} joueurs cotés"),
            )
        return None

    def odds(
        self,
        comp: Competition,
        home: str,
        away: str,
        diagnostics: OddsDiagnostics | None = None,
    ) -> OddsSnapshot | None:
        """Cotes du match, avec traçabilité de chaque étape.

        Toute sortie sans cotes enregistre **pourquoi** : clé absente,
        compétition non couverte, match hors calendrier, quota épuisé,
        réponse vide. L'appelant peut ainsi l'expliquer à l'utilisateur au
        lieu d'afficher un « indisponible » muet.
        """
        diag = diagnostics if diagnostics is not None else OddsDiagnostics()

        if not self.enabled:
            stage = "no_key" if not self.api_key else "source_disabled"
            diag.add(self.name, stage)
            log.info("cotes %s vs %s : %s", home, away, ODDS_REASONS[stage])
            return None
        if comp.sport not in self.supports:
            diag.add(self.name, "competition_unsupported", f"{comp.label} hors périmètre")
            return None

        sport_keys = self.resolve_keys(comp)
        if not sport_keys:
            diag.add(self.name, "no_sport_key", f"{comp.label} inconnue du fournisseur")
            log.info("cotes %s : aucune clé fournisseur pour %s", comp.label, comp.key)
            return None

        markets = self.MARKETS.get(comp.sport, "h2h")
        for key in sport_keys:
            got = self._events(key, comp.scope)
            if not got:
                diag.add(self.name, "no_events", f"{key} : calendrier vide ou injoignable")
                continue

            event, score = self._find_event(got[0], home, away)
            if event is None:
                diag.add(
                    self.name, "event_not_found",
                    f"{key} : {len(got[0])} match(s) au calendrier, "
                    f"aucun ne correspond (meilleure concordance {score:.0%})",
                )
                log.info(
                    "cotes : %s vs %s absent de %s (%d événements, score %.2f)",
                    home, away, key, len(got[0]), score,
                )
                continue

            snapshot = self._fetch_event_odds(comp, key, event, markets, diag)
            if snapshot is not None:
                diag.add(self.name, "success",
                         f"{snapshot.bookmaker_count} bookmaker(s) via {key}", success=True)
                return snapshot
        return None

    def _fetch_event_odds(
        self, comp: Competition, key: str, event: dict, markets: str,
        diag: OddsDiagnostics,
    ) -> OddsSnapshot | None:
        """Interroge les cotes, en essayant plusieurs régions si nécessaire."""
        regions = [
            r.strip() for r in os.getenv("ODDS_REGIONS", "eu,uk,us").split(",") if r.strip()
        ]
        for region in regions:
            res = self.http.get_json(
                f"{self.BASE}/sports/{key}/odds",
                params={
                    "apiKey": self.api_key,
                    "regions": region,
                    "markets": markets,
                    "oddsFormat": "decimal",
                    "dateFormat": "iso",
                    "eventIds": event.get("id", ""),
                },
                ttl=cfg.TTL.odds,
                provider=self.name,
                cost=len(markets.split(",")),
                scope=comp.scope,
            )
            if not res:
                error = self.http.last_error or ""
                stage = (
                    "quota_exhausted" if "quota" in error
                    else ("network_error" if error.startswith("network") else "empty_response")
                )
                diag.add(self.name, stage, f"région {region} : {error or 'aucune réponse'}")
                if stage == "quota_exhausted":
                    return None  # inutile d'essayer les autres régions
                continue

            payload, ts, from_cache = res
            rows = payload if isinstance(payload, list) else []
            row = next((r for r in rows if r.get("id") == event.get("id")), None)
            if row is None:
                diag.add(self.name, "empty_response", f"région {region} : réponse sans ce match")
                continue

            snapshot = self._parse_odds(row, key, ts, from_cache, comp)
            if not snapshot.has_h2h:
                diag.add(
                    self.name, "no_market",
                    f"région {region} : {snapshot.bookmaker_count} bookmaker(s) "
                    "mais aucun marché vainqueur",
                )
                continue

            self._attach_drift(comp, snapshot, from_cache)
            return snapshot
        return None

    def _attach_drift(self, comp: Competition, snapshot: OddsSnapshot, from_cache: bool) -> None:
        if self.history is None:
            return
        drift = (
            self.history.record(comp, snapshot)
            if not from_cache
            else self.history.drift(comp, snapshot)
        )
        snapshot.movement = drift.get("movement", {})
        snapshot.movement_hours = drift.get("hours")

    # Concordance minimale exigée sur la PAIRE d'équipes (somme de deux
    # similarités, donc sur 2). 1.55 laissait passer des appariements
    # douteux ; 1.70 exige que les deux noms concordent vraiment.
    EVENT_MATCH_MIN = 1.70

    @classmethod
    def _find_event(
        cls, events: list[dict], home: str, away: str
    ) -> tuple[dict | None, float]:
        """Retrouve le match, et renvoie aussi la qualité de concordance.

        Renvoyer le score permet de dire « meilleure concordance 62 % » quand
        rien ne correspond, au lieu d'un échec silencieux.
        """
        best, best_score = None, 0.0
        for ev in events:
            h, a = ev.get("home_team", ""), ev.get("away_team", "")
            direct = name_similarity(home, h) + name_similarity(away, a)
            swapped = name_similarity(home, a) + name_similarity(away, h)
            score = max(direct, swapped)
            if score > best_score:
                best, best_score = ev, score
        normalised = best_score / 2.0
        return (best, normalised) if best_score >= cls.EVENT_MATCH_MIN else (None, normalised)

    def _parse_odds(
        self, row: dict, sport_key: str, ts: float, from_cache: bool, comp: Competition
    ) -> OddsSnapshot:
        """Consensus du marché = moyenne des cotes de tous les bookmakers."""
        h2h_acc: dict[str, list[float]] = {}
        totals_acc: dict[float, dict[str, list[float]]] = {}
        spreads_acc: dict[float, dict[str, list[float]]] = {}
        btts_acc: dict[str, list[float]] = {}
        per_book: dict[str, dict[str, float]] = {}
        books = row.get("bookmakers") or []

        for book in books:
            book_name = book.get("title") or book.get("key") or "?"
            for market in book.get("markets") or []:
                mkey = market.get("key")
                for out in market.get("outcomes") or []:
                    price = _to_float(out.get("price"))
                    if not price or price <= 1.0:
                        continue
                    label = str(out.get("name", ""))
                    point = _to_float(out.get("point"))
                    if mkey == "h2h":
                        h2h_acc.setdefault(label, []).append(price)
                        per_book.setdefault(book_name, {})[label] = price
                    elif mkey == "btts":
                        # « Les deux équipes marquent » : indexé sur une ligne
                        # fictive pour réutiliser la structure des totaux.
                        btts_acc.setdefault(label, []).append(price)
                    elif mkey == "totals" and point is not None:
                        totals_acc.setdefault(point, {}).setdefault(label, []).append(price)
                    elif mkey == "spreads" and point is not None:
                        # On indexe par la ligne vue du côté domicile.
                        line = point if label == row.get("home_team") else -point
                        spreads_acc.setdefault(line, {}).setdefault(label, []).append(price)

        def mean_map(acc: dict) -> dict:
            return {k: consensus_price(v) for k, v in acc.items() if v}

        return OddsSnapshot(
            home_team=row.get("home_team", ""),
            away_team=row.get("away_team", ""),
            commence_time=_parse_dt(row.get("commence_time")),
            sport_key=sport_key,
            provenance=self._prov(
                ts, from_cache, f"{comp.label} · {len(books)} bookmakers"
            ),
            h2h=mean_map(h2h_acc),
            totals={line: mean_map(vals) for line, vals in totals_acc.items()},
            spreads={line: mean_map(vals) for line, vals in spreads_acc.items()},
            bookmaker_count=len(books),
            per_book_h2h=per_book,
            btts=mean_map(btts_acc),
        )


# --------------------------------------------------------------------------
# Historique des cotes (évolution avant le coup d'envoi — §9)
# --------------------------------------------------------------------------
class OddsHistory:
    """Conserve les relevés de cotes pour mesurer leur dérive."""

    def __init__(self, path=None, max_events: int = 400, window_hours: float = 96.0):
        self.path = str(path or cfg.ODDS_HISTORY_FILE)
        self.max_events = max_events
        self.window_hours = window_hours
        self._lock = threading.Lock()

    def _load(self) -> dict[str, list[dict]]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def _key(comp: Competition, snap: OddsSnapshot) -> str:
        return f"{comp.scope}|{normalize_name(snap.home_team)}|{normalize_name(snap.away_team)}"

    def record(self, comp: Competition, snap: OddsSnapshot) -> dict:
        with self._lock:
            data = self._load()
            key = self._key(comp, snap)
            rows = data.get(key, [])
            rows.append({"ts": time.time(), "h2h": snap.h2h})
            rows = rows[-12:]
            data[key] = rows
            if len(data) > self.max_events:
                ordered = sorted(
                    data.items(), key=lambda kv: kv[1][-1].get("ts", 0) if kv[1] else 0
                )
                data = dict(ordered[-self.max_events :])
            self._save(data)
        return self._compute(rows, snap)

    def drift(self, comp: Competition, snap: OddsSnapshot) -> dict:
        rows = self._load().get(self._key(comp, snap), [])
        return self._compute(rows, snap)

    def _compute(self, rows: list[dict], snap: OddsSnapshot) -> dict:
        """Variation relative des cotes depuis le plus ancien relevé utile."""
        now = time.time()
        usable = [
            r for r in rows if now - r.get("ts", 0) <= self.window_hours * 3600
        ]
        if len(usable) < 2:
            return {}
        oldest = usable[0]
        movement: dict[str, float] = {}
        for outcome, price in snap.h2h.items():
            before = (oldest.get("h2h") or {}).get(outcome)
            if before and price:
                movement[outcome] = (price - before) / before
        if not movement:
            return {}
        return {"movement": movement, "hours": (now - oldest["ts"]) / 3600.0}


# --------------------------------------------------------------------------
# 2. TheSportsDB — repli universel (gratuit, sans clé obligatoire)
# --------------------------------------------------------------------------
class SportApi7Provider(BaseProvider):
    """Statistiques de saison : corners, tirs, possession.

    Comble le manque le plus visible du modèle — les corners, jusqu'ici
    affichés « données insuffisantes ». Les tirs cadrés servent de substitut
    stable aux xG, qui n'apparaissent pas dans l'agrégat de saison.

    **Quota très étroit : 50 requêtes par mois.** Toute la conception en
    découle. Récupérer les statistiques match par match coûterait une
    vingtaine d'appels par analyse et épuiserait le mois en deux rencontres.
    On ne prend donc que l'agrégat de saison — trois appels pour une équipe
    jamais vue, zéro ensuite grâce au cache.

    Ce fournisseur n'implémente volontairement pas `form()` : il ne connaît
    pas le détail des rencontres et entrerait en concurrence avec des
    sources qui, elles, le fournissent. Il complète, il ne remplace pas.
    """

    name = "sportapi7"
    label = "Statistiques avancées de saison"
    supports = frozenset({"football"})
    BASE = "https://sportapi7.p.rapidapi.com/api/v1"

    def __init__(self, http: HttpClient, api_key: str = ""):
        super().__init__(http)
        self.api_key = api_key
        self.enabled = bool(api_key) and cfg.SOURCES.sportapi7

    def handles(self, comp: Competition) -> bool:
        return self.enabled and comp.sport == "football" and not comp.is_cup

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "x-rapidapi-key": self.api_key,
            "x-rapidapi-host": "sportapi7.p.rapidapi.com",
        }

    def _team_id(self, team: str, scope: str) -> int | None:
        """Identifiant de l'équipe. Mis en cache très longtemps : il ne change
        pas d'une saison à l'autre, et chaque appel coûte 2 % du quota."""
        res = self.http.get_json(
            f"{self.BASE}/search/all", params={"q": team},
            headers=self._headers, ttl=cfg.TTL.catalog, provider=self.name,
            scope=scope,
        )
        if not res:
            return None
        candidats = []
        for entree in (res[0] or {}).get("results") or []:
            if entree.get("type") != "team":
                continue
            e = entree.get("entity") or {}
            if ((e.get("sport") or {}).get("name") or "").lower() != "football":
                continue
            if e.get("id") and e.get("name"):
                candidats.append((str(e["name"]), int(e["id"])))
        if not candidats:
            return None
        # Plusieurs clubs portent le même nom : on retient le plus proche.
        meilleur = best_match(team, [n for n, _ in candidats], threshold=0.70)
        for nom, ident in candidats:
            if nom == meilleur:
                return ident
        return candidats[0][1]

    # Saisons essayées, de la plus récente à la précédente. Deux suffisent :
    # à l'intersaison, la saison ouverte ne compte aucun match et il faut se
    # rabattre sur celle qui vient de s'achever.
    SEASONS_TESTEES = 2

    def _seasons(self, team_id: int, comp: Competition) -> list[tuple[int, int]]:
        """Couples (tournoi, saison) candidats, du plus récent au plus ancien.

        Résolus dynamiquement plutôt que codés en dur : les identifiants
        changent chaque année, et une valeur périmée provoquerait un appel
        payant sans résultat.
        """
        res = self.http.get_json(
            f"{self.BASE}/team/{team_id}/standings/seasons",
            headers=self._headers, ttl=cfg.TTL.catalog, provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return []
        for bloc in (res[0] or {}).get("tournamentSeasons") or []:
            tournoi = (bloc.get("tournament") or {}).get("uniqueTournament") or {}
            if name_similarity(str(tournoi.get("name") or ""), comp.label) < 0.75:
                continue
            saisons = bloc.get("seasons") or []
            if not tournoi.get("id") or not saisons:
                continue
            return [
                (int(tournoi["id"]), int(s["id"]))
                for s in saisons[: self.SEASONS_TESTEES] if s.get("id")
            ]
        return []

    def season_profile(self, comp: Competition, team: str) -> dict[str, float] | None:
        """Moyennes par match de la saison, ou None si indisponible.

        Les totaux bruts sont ramenés au match : le moteur raisonne en
        moyennes, et comparer un total de 216 corners sur 38 journées à un
        autre sur 25 n'aurait aucun sens.
        """
        if not self.handles(comp):
            return None
        team_id = self._team_id(team, comp.scope)
        if team_id is None:
            return None

        brut, joues = {}, 0.0
        for tournoi, saison in self._seasons(team_id, comp):
            res = self.http.get_json(
                f"{self.BASE}/team/{team_id}/unique-tournament/{tournoi}"
                f"/season/{saison}/statistics/overall",
                headers=self._headers, ttl=cfg.TTL.standings, provider=self.name,
                scope=comp.scope,
            )
            if not res:
                continue
            candidat = (res[0] or {}).get("statistics") or {}
            disputes = _to_float(candidat.get("matches")) or 0.0
            # À l'intersaison, la saison ouverte affiche zéro match : on
            # passe à la précédente plutôt que de renoncer.
            if disputes >= 5:
                brut, joues = candidat, disputes
                break
        if not brut:
            return None

        def par_match(cle: str) -> float | None:
            valeur = _to_float(brut.get(cle))
            return valeur / joues if valeur is not None else None

        profil = {
            "corners_for": par_match("corners"),
            "shots": par_match("shots"),
            "shots_on_target": par_match("shotsOnTarget"),
            "shots_against": par_match("shotsAgainst"),
            "shots_on_target_against": par_match("shotsOnTargetAgainst"),
            "big_chances": par_match("bigChances"),
        }
        possession = _to_float(brut.get("averageBallPossession"))
        if possession is not None:
            profil["possession"] = possession
        profil["season_sample"] = joues
        return {k: v for k, v in profil.items() if v is not None}


class TheSportsDbProvider(BaseProvider):
    name = "thesportsdb"
    label = "Base sportive publique"
    supports = frozenset({"football", "basket", "tennis", "hockey"})

    def __init__(self, http: HttpClient, api_key: str = "3"):
        super().__init__(http)
        self.api_key = api_key or "3"
        self.enabled = cfg.SOURCES.thesportsdb

    @property
    def BASE(self) -> str:
        return f"https://www.thesportsdb.com/api/v1/json/{self.api_key}"

    def handles(self, comp: Competition) -> bool:
        return self.enabled and bool(comp.sportsdb_league or comp.team_pool)

    def _league_teams(self, league_name: str, scope: str) -> tuple[list[str], float, bool] | None:
        # `search_all_teams.php?l=` est le seul endpoint « toutes équipes »
        # qui renvoie de vraies données sur le palier gratuit.
        res = self.http.get_json(
            f"{self.BASE}/search_all_teams.php",
            params={"l": league_name},
            ttl=cfg.TTL.catalog,
            scope=scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        names = [
            t["strTeam"].strip()
            for t in ((payload or {}).get("teams") or [])
            if t.get("strTeam")
        ]
        return (names, ts, from_cache) if names else None

    def _league_id(self, league_name: str, scope: str) -> str | None:
        """Identifiant de la ligue, lu dans la réponse « toutes équipes ».

        Chaque équipe porte son `idLeague` : inutile d'interroger un endpoint
        supplémentaire, la réponse est déjà en cache pour les effectifs.
        """
        res = self.http.get_json(
            f"{self.BASE}/search_all_teams.php",
            params={"l": league_name},
            ttl=cfg.TTL.catalog,
            scope=scope,
        )
        if not res:
            return None
        for equipe in ((res[0] or {}).get("teams") or []):
            identifiant = str(equipe.get("idLeague") or "").strip()
            if identifiant:
                return identifiant
        return None

    # Journées lues au-delà de la prochaine. Le palier gratuit plafonne à cinq
    # matchs par appel : enchaîner quelques journées est le seul moyen d'obtenir
    # un calendrier utilisable sans clé.
    #
    # Le chiffre est volontairement bas. TheSportsDB limite le débit du palier
    # gratuit et répond 429 dès qu'on insiste : mieux vaut trois journées
    # fiables qu'une rafale d'appels refusés. Le cache absorbe le reste.
    ROUNDS_AHEAD = 2

    @staticmethod
    def _event_to_fixture(ev: dict) -> "Fixture | None":
        domicile = str(ev.get("strHomeTeam") or "").strip()
        exterieur = str(ev.get("strAwayTeam") or "").strip()
        if not domicile or not exterieur:
            return None
        jour = ev.get("dateEvent")
        heure = (ev.get("strTime") or "00:00:00")[:8]
        return Fixture(
            home=domicile,
            away=exterieur,
            starts_at=_parse_dt(f"{jour}T{heure}Z") if jour else None,
        )

    def fixtures(self, comp: Competition) -> list[Fixture]:
        """Prochaines rencontres. Repli gratuit quand aucune clé de cotes n'est posée.

        `eventsnextleague` ne rend qu'un seul match — trop peu pour choisir.
        On lui demande donc surtout *quelle journée* vient, puis on lit cette
        journée et les suivantes via `eventsround`, qui en rend cinq à chaque
        fois. Le calendrier devient utilisable sans clé ni quota.

        Les rencontres déjà jouées sont écartées : une journée en cours contient
        des matchs passés, qu'il serait absurde de proposer à la simulation.
        """
        if not self.handles(comp) or not comp.sportsdb_league:
            return []
        identifiant = self._league_id(comp.sportsdb_league, comp.scope)
        if not identifiant:
            return []

        res = self.http.get_json(
            f"{self.BASE}/eventsnextleague.php",
            params={"id": identifiant},
            ttl=cfg.TTL.events,
            scope=comp.scope,
        )
        prochains = ((res[0] if res else None) or {}).get("events") or []
        if not prochains:
            return []

        sorties: list[Fixture] = []
        for ev in prochains:
            trouve = self._event_to_fixture(ev)
            if trouve:
                sorties.append(trouve)

        # La journée du prochain match sert de point de départ.
        try:
            journee = int(prochains[0].get("intRound") or 0)
        except (TypeError, ValueError):
            journee = 0
        saison = str(prochains[0].get("strSeason") or "").strip()
        if not journee or not saison:
            return sorties

        maintenant = datetime.now(UTC)
        for suivante in range(journee, journee + self.ROUNDS_AHEAD + 1):
            res = self.http.get_json(
                f"{self.BASE}/eventsround.php",
                params={"id": identifiant, "r": suivante, "s": saison},
                ttl=cfg.TTL.events,
                scope=comp.scope,
            )
            if not res:
                # Débit dépassé : insister ne ferait qu'aggraver le blocage.
                # On garde ce qui a déjà été récolté.
                if "429" in (self.http.last_error or ""):
                    log.info("thesportsdb : débit dépassé, arrêt à la journée %d", suivante)
                    break
                continue
            for ev in (res[0] or {}).get("events") or []:
                trouve = self._event_to_fixture(ev)
                if trouve is None:
                    continue
                if trouve.starts_at is not None and trouve.starts_at < maintenant:
                    continue          # déjà joué : hors sujet pour une simulation
                sorties.append(trouve)
        return sorties

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        names: set[str] = set()
        newest, cached_all = 0.0, True
        sources: list[str] = []

        if comp.sportsdb_league:
            got = self._league_teams(comp.sportsdb_league, comp.scope)
            if got:
                names.update(got[0])
                newest, cached_all = max(newest, got[1]), cached_all and got[2]
                sources.append(comp.sportsdb_league)

        # Coupes : les participants viennent des championnats nourriciers.
        if not names and comp.team_pool:
            for pool_key in comp.team_pool:
                pool = cfg.competition(comp.sport, pool_key)
                if not pool or not pool.sportsdb_league:
                    continue
                got = self._league_teams(pool.sportsdb_league, comp.scope)
                if got:
                    names.update(got[0])
                    newest, cached_all = max(newest, got[1]), cached_all and got[2]
                    sources.append(pool.sportsdb_league)

        if not names:
            return None
        detail = f"{comp.label} · {len(sources)} ligue(s)"
        return sorted(names), self._prov(newest or time.time(), cached_all, detail)

    def _team_id(self, team: str, scope: str) -> str | None:
        res = self.http.get_json(
            f"{self.BASE}/searchteams.php",
            params={"t": team},
            ttl=cfg.TTL.catalog,
            scope=scope,
        )
        if not res:
            return None
        teams = (res[0] or {}).get("teams") or []
        if not teams:
            return None
        names = [t.get("strTeam", "") for t in teams]
        match = best_match(team, names, threshold=0.6) or names[0]
        for t in teams:
            if t.get("strTeam") == match:
                return t.get("idTeam")
        return None

    def form(self, comp: Competition, team: str) -> TeamForm | None:
        if not self.handles(comp):
            return None
        team_id = self._team_id(team, comp.scope)
        if not team_id:
            return None
        res = self.http.get_json(
            f"{self.BASE}/eventslast.php",
            params={"id": team_id},
            ttl=cfg.TTL.form,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        events = (payload or {}).get("results") or []
        matches: list[MatchResult] = []
        for ev in events[: cfg.FORM_WINDOW]:
            hs, aws = _to_float(ev.get("intHomeScore")), _to_float(ev.get("intAwayScore"))
            if hs is None or aws is None:
                continue
            is_home = str(ev.get("idHomeTeam")) == str(team_id)
            matches.append(
                MatchResult(
                    date=_parse_dt(ev.get("dateEvent")) or datetime.now(UTC),
                    opponent=(ev.get("strAwayTeam") if is_home else ev.get("strHomeTeam")) or "?",
                    home=is_home,
                    scored=hs if is_home else aws,
                    conceded=aws if is_home else hs,
                    competition=ev.get("strLeague") or "",
                )
            )
        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        return TeamForm(
            team=team,
            sport=comp.sport,
            matches=matches,
            provenance=self._prov(ts, from_cache, f"{len(matches)} derniers matchs"),
        )

    def venue(self, comp: Competition, team: str) -> str | None:
        """Localisation du stade — sert au bulletin météo."""
        if not self.enabled:
            return None
        res = self.http.get_json(
            f"{self.BASE}/searchteams.php",
            params={"t": team},
            ttl=cfg.TTL.catalog,
            scope=comp.scope,
        )
        if not res:
            return None
        teams = (res[0] or {}).get("teams") or []
        if not teams:
            return None
        entry = teams[0]
        return entry.get("strStadiumLocation") or entry.get("strLocation") or None


# --------------------------------------------------------------------------
# 3. football-data.org — football, grandes ligues
# --------------------------------------------------------------------------
class FootballDataProvider(BaseProvider):
    name = "football_data"
    label = "Résultats officiels du championnat"
    supports = frozenset({"football"})
    BASE = "https://api.football-data.org/v4"

    def __init__(self, http: HttpClient, api_key: str = ""):
        super().__init__(http)
        self.api_key = api_key
        self.enabled = bool(api_key) and cfg.SOURCES.football_data

    def handles(self, comp: Competition) -> bool:
        return self.enabled and bool(comp.football_data_code)

    @property
    def _headers(self) -> dict:
        return {"X-Auth-Token": self.api_key}

    def _teams_catalog(self, comp: Competition) -> tuple[dict[str, int], float, bool] | None:
        """Équipes de LA compétition demandée uniquement (§7)."""
        res = self.http.get_json(
            f"{self.BASE}/competitions/{comp.football_data_code}/teams",
            headers=self._headers,
            ttl=cfg.TTL.catalog,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        catalog: dict[str, int] = {}
        for team in (payload or {}).get("teams") or []:
            for key in ("name", "shortName"):
                if team.get(key):
                    catalog.setdefault(team[key], team.get("id"))
        return (catalog, ts, from_cache) if catalog else None

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        got = self._teams_catalog(comp)
        if not got:
            return None
        catalog, ts, from_cache = got
        # On ne garde que les noms longs pour éviter les doublons d'affichage.
        names = sorted({n for n in catalog if len(n) > 3})
        return names, self._prov(ts, from_cache, f"{comp.label} · équipes engagées")

    def form(self, comp: Competition, team: str) -> TeamForm | None:
        if not self.handles(comp):
            return None
        got = self._teams_catalog(comp)
        if not got:
            return None
        catalog, _ts, _cached = got
        match_name = best_match(team, catalog.keys())
        if not match_name:
            return None
        team_id = catalog[match_name]
        res = self.http.get_json(
            f"{self.BASE}/teams/{team_id}/matches",
            params={"status": "FINISHED", "limit": cfg.FORM_WINDOW},
            headers=self._headers,
            ttl=cfg.TTL.form,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        matches: list[MatchResult] = []
        for ev in (payload or {}).get("matches") or []:
            score = (ev.get("score") or {}).get("fullTime") or {}
            hs, aws = _to_float(score.get("home")), _to_float(score.get("away"))
            if hs is None or aws is None:
                continue
            is_home = ((ev.get("homeTeam") or {}).get("id")) == team_id
            opp = (ev.get("awayTeam") if is_home else ev.get("homeTeam")) or {}
            matches.append(
                MatchResult(
                    date=_parse_dt(ev.get("utcDate")) or datetime.now(UTC),
                    opponent=opp.get("name", "?"),
                    home=is_home,
                    scored=hs if is_home else aws,
                    conceded=aws if is_home else hs,
                    competition=(ev.get("competition") or {}).get("name", ""),
                )
            )
        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        return TeamForm(
            team=match_name,
            sport=comp.sport,
            matches=matches[: cfg.FORM_WINDOW],
            provenance=self._prov(ts, from_cache, f"{len(matches)} matchs terminés"),
        )

    def standings(self, comp: Competition) -> tuple[dict[str, Standing], Provenance] | None:
        if not self.handles(comp) or comp.is_cup:
            return None
        res = self.http.get_json(
            f"{self.BASE}/competitions/{comp.football_data_code}/standings",
            headers=self._headers,
            ttl=cfg.TTL.standings,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        table: dict[str, Standing] = {}
        for block in (payload or {}).get("standings") or []:
            if block.get("type") != "TOTAL":
                continue
            for row in block.get("table") or []:
                name = (row.get("team") or {}).get("name")
                if not name:
                    continue
                table[name] = Standing(
                    team=name,
                    rank=int(row.get("position") or 0),
                    played=int(row.get("playedGames") or 0),
                    points=int(row.get("points") or 0),
                    goals_for=int(row.get("goalsFor") or 0),
                    goals_against=int(row.get("goalsAgainst") or 0),
                    won=int(row.get("won") or 0),
                    drawn=int(row.get("draw") or 0),
                    lost=int(row.get("lost") or 0),
                )
        if not table:
            return None
        return table, self._prov(ts, from_cache, f"{comp.label} · classement")


# --------------------------------------------------------------------------
# 4. API-Football (RapidAPI) — statistiques riches (xG, corners, tirs…)
# --------------------------------------------------------------------------
class ApiFootballProvider(BaseProvider):
    name = "api_football"
    label = "Statistiques officielles du championnat"
    supports = frozenset({"football"})
    BASE = "https://api-football-v1.p.rapidapi.com/v3"
    HOST = "api-football-v1.p.rapidapi.com"

    # Correspondance libellé API → clé interne de `MatchResult.extra`.
    _STAT_MAP = {
        "corner kicks": "corners",
        "yellow cards": "yellow_cards",
        "red cards": "red_cards",
        "ball possession": "possession",
        "total shots": "shots",
        "shots on goal": "shots_on_target",
        "expected_goals": "xg",
        "expected goals": "xg",
    }

    def __init__(self, http: HttpClient, api_key: str = ""):
        super().__init__(http)
        self.api_key = api_key
        self.enabled = bool(api_key) and cfg.SOURCES.api_football
        # Les stats détaillées coûtent 1 appel par match : on borne la fenêtre.
        self.detail_window = cfg.FORM_WINDOW if cfg.PREMIUM_MODE else 6

    def handles(self, comp: Competition) -> bool:
        return self.enabled and comp.api_football_id is not None

    @property
    def _headers(self) -> dict:
        return {"X-RapidAPI-Key": self.api_key, "X-RapidAPI-Host": self.HOST}

    def _teams_catalog(self, comp: Competition) -> tuple[dict[str, int], float, bool] | None:
        """Équipes engagées dans la compétition et la saison en cours."""
        res = self.http.get_json(
            f"{self.BASE}/teams",
            params={"league": comp.api_football_id, "season": cfg.european_season()},
            headers=self._headers,
            ttl=cfg.TTL.catalog,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        catalog: dict[str, int] = {}
        for item in (payload or {}).get("response") or []:
            team = item.get("team") or {}
            if team.get("name") and team.get("id"):
                catalog[team["name"]] = team["id"]
        return (catalog, ts, from_cache) if catalog else None

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        got = self._teams_catalog(comp)
        if not got:
            return None
        catalog, ts, from_cache = got
        return sorted(catalog), self._prov(
            ts, from_cache, f"{comp.label} · {len(catalog)} équipes"
        )

    def _team_id(self, comp: Competition, team: str) -> int | None:
        got = self._teams_catalog(comp)
        if got:
            chosen = best_match(team, got[0].keys(), threshold=0.65)
            if chosen:
                return got[0][chosen]
        # Repli : recherche par nom, hors compétition.
        res = self.http.get_json(
            f"{self.BASE}/teams",
            params={"search": team},
            headers=self._headers,
            ttl=cfg.TTL.catalog,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        items = (res[0] or {}).get("response") or []
        names = [(it.get("team") or {}).get("name", "") for it in items]
        chosen = best_match(team, names, threshold=0.6)
        for it in items:
            if (it.get("team") or {}).get("name") == chosen:
                return (it.get("team") or {}).get("id")
        return (items[0].get("team") or {}).get("id") if items else None

    def form(self, comp: Competition, team: str) -> TeamForm | None:
        if not self.handles(comp):
            return None
        team_id = self._team_id(comp, team)
        if not team_id:
            return None
        res = self.http.get_json(
            f"{self.BASE}/fixtures",
            params={"team": team_id, "last": cfg.FORM_WINDOW},
            headers=self._headers,
            ttl=cfg.TTL.form,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        matches: list[MatchResult] = []
        for fx in (payload or {}).get("response") or []:
            goals = fx.get("goals") or {}
            hs, aws = _to_float(goals.get("home")), _to_float(goals.get("away"))
            if hs is None or aws is None:
                continue
            teams = fx.get("teams") or {}
            is_home = ((teams.get("home") or {}).get("id")) == team_id
            opp = (teams.get("away") if is_home else teams.get("home")) or {}
            matches.append(
                MatchResult(
                    date=_parse_dt((fx.get("fixture") or {}).get("date")) or datetime.now(UTC),
                    opponent=opp.get("name", "?"),
                    home=is_home,
                    scored=hs if is_home else aws,
                    conceded=aws if is_home else hs,
                    competition=(fx.get("league") or {}).get("name", ""),
                    extra={"fixture_id": (fx.get("fixture") or {}).get("id") or 0},
                )
            )
        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        self._enrich_stats(matches, team_id, comp)
        form = TeamForm(
            team=team,
            sport=comp.sport,
            matches=matches,
            provenance=self._prov(ts, from_cache, f"{len(matches)} matchs + statistiques"),
        )
        return form

    def _enrich_stats(
        self, matches: list[MatchResult], team_id: int, comp: Competition
    ) -> None:
        """Corners, cartons, tirs, possession, xG — jamais inventés (§9)."""
        status = self.http.quota.status(self.name)
        budget = self.detail_window
        if status is not None:
            budget = min(budget, max(0, status.remaining - 10))

        for m in matches[: self.detail_window]:
            fixture_id = int(m.extra.get("fixture_id") or 0)
            if not fixture_id:
                continue
            url = f"{self.BASE}/fixtures/statistics"
            params = {"fixture": fixture_id}
            key = CacheStore.make_key(url, params, "json")
            if self.http.cache.get(key, cfg.TTL.stats) is None:
                if budget <= 0:
                    continue
                budget -= 1
            res = self.http.get_json(
                url,
                params=params,
                headers=self._headers,
                ttl=cfg.TTL.stats,  # une stat de match passé ne change plus
                provider=self.name,
                scope=comp.scope,
            )
            if not res:
                continue
            for block in (res[0] or {}).get("response") or []:
                is_team = ((block.get("team") or {}).get("id")) == team_id
                for stat in block.get("statistics") or []:
                    label = (stat.get("type") or "").strip().lower()
                    field_name = self._STAT_MAP.get(label)
                    if not field_name:
                        continue
                    value = _to_float(stat.get("value"))
                    if value is None:
                        continue
                    suffix = "for" if is_team else "against"
                    if field_name in {"yellow_cards", "red_cards", "possession",
                                      "shots", "shots_on_target"}:
                        if is_team:
                            m.extra[field_name] = value
                    else:
                        m.extra[f"{field_name}_{suffix}"] = value
            if "corners_for" in m.extra and "corners_against" in m.extra:
                m.extra["corners_total"] = m.extra["corners_for"] + m.extra["corners_against"]

    def standings(self, comp: Competition) -> tuple[dict[str, Standing], Provenance] | None:
        if not self.handles(comp) or comp.is_cup:
            return None
        res = self.http.get_json(
            f"{self.BASE}/standings",
            params={"league": comp.api_football_id, "season": cfg.european_season()},
            headers=self._headers,
            ttl=cfg.TTL.standings,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        table: dict[str, Standing] = {}
        for league in (payload or {}).get("response") or []:
            for group in ((league.get("league") or {}).get("standings") or []):
                for row in group:
                    name = (row.get("team") or {}).get("name")
                    stats = row.get("all") or {}
                    goals = stats.get("goals") or {}
                    if not name:
                        continue
                    table[name] = Standing(
                        team=name,
                        rank=int(row.get("rank") or 0),
                        played=int(stats.get("played") or 0),
                        points=int(row.get("points") or 0),
                        goals_for=int(goals.get("for") or 0),
                        goals_against=int(goals.get("against") or 0),
                        won=int(stats.get("win") or 0),
                        drawn=int(stats.get("draw") or 0),
                        lost=int(stats.get("lose") or 0),
                    )
        if not table:
            return None
        return table, self._prov(ts, from_cache, f"{comp.label} · classement")

    def head_to_head(
        self, comp: Competition, home: str, away: str
    ) -> tuple[list[MatchResult], Provenance] | None:
        if not self.handles(comp):
            return None
        id_home, id_away = self._team_id(comp, home), self._team_id(comp, away)
        if not id_home or not id_away:
            return None
        res = self.http.get_json(
            f"{self.BASE}/fixtures/headtohead",
            params={"h2h": f"{id_home}-{id_away}", "last": 10},
            headers=self._headers,
            ttl=cfg.TTL.form,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        out: list[MatchResult] = []
        for fx in (payload or {}).get("response") or []:
            goals = fx.get("goals") or {}
            hs, aws = _to_float(goals.get("home")), _to_float(goals.get("away"))
            if hs is None or aws is None:
                continue
            teams = fx.get("teams") or {}
            is_home = ((teams.get("home") or {}).get("id")) == id_home
            out.append(
                MatchResult(
                    date=_parse_dt((fx.get("fixture") or {}).get("date")) or datetime.now(UTC),
                    opponent=away,
                    home=is_home,
                    scored=hs if is_home else aws,
                    conceded=aws if is_home else hs,
                    competition=(fx.get("league") or {}).get("name", ""),
                )
            )
        if not out:
            return None
        out.sort(key=lambda m: m.date, reverse=True)
        return out, self._prov(ts, from_cache, "confrontations directes")


# --------------------------------------------------------------------------
# 5. balldontlie — NBA
# --------------------------------------------------------------------------
class BallDontLieProvider(BaseProvider):
    name = "balldontlie"
    label = "Données officielles NBA"
    supports = frozenset({"basket"})
    BASE = "https://api.balldontlie.io/v1"

    def __init__(self, http: HttpClient, api_key: str = ""):
        super().__init__(http)
        self.api_key = api_key
        self.enabled = bool(api_key) and cfg.SOURCES.balldontlie

    def handles(self, comp: Competition) -> bool:
        return self.enabled and comp.balldontlie

    @property
    def _headers(self) -> dict:
        return {"Authorization": self.api_key}

    def _teams(self, comp: Competition) -> tuple[dict[str, dict], float, bool] | None:
        res = self.http.get_json(
            f"{self.BASE}/teams",
            headers=self._headers,
            params={"per_page": 100},
            ttl=cfg.TTL.catalog,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        teams = {t["full_name"]: t for t in ((payload or {}).get("data") or []) if t.get("full_name")}
        return (teams, ts, from_cache) if teams else None

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        got = self._teams(comp)
        if not got:
            return None
        teams, ts, from_cache = got
        return sorted(teams), self._prov(ts, from_cache, f"{comp.label} · franchises")

    def form(self, comp: Competition, team: str) -> TeamForm | None:
        if not self.handles(comp):
            return None
        got = self._teams(comp)
        if not got:
            return None
        teams, _ts, _c = got
        chosen = best_match(team, teams.keys())
        if not chosen:
            return None
        team_id = teams[chosen].get("id")
        end = datetime.now(UTC).date()
        start = end - timedelta(days=120)
        res = self.http.get_json(
            f"{self.BASE}/games",
            headers=self._headers,
            params={
                "team_ids[]": team_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "per_page": 100,
            },
            ttl=cfg.TTL.form,
            provider=self.name,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        games = [g for g in ((payload or {}).get("data") or []) if g.get("status") == "Final"]
        matches: list[MatchResult] = []
        for g in games:
            hs, aws = _to_float(g.get("home_team_score")), _to_float(g.get("visitor_team_score"))
            if hs is None or aws is None or (hs == 0 and aws == 0):
                continue
            is_home = ((g.get("home_team") or {}).get("id")) == team_id
            opp = (g.get("visitor_team") if is_home else g.get("home_team")) or {}
            matches.append(
                MatchResult(
                    date=_parse_dt(g.get("date")) or datetime.now(UTC),
                    opponent=opp.get("full_name", "?"),
                    home=is_home,
                    scored=hs if is_home else aws,
                    conceded=aws if is_home else hs,
                    competition=comp.label,
                )
            )
        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        return TeamForm(
            team=chosen,
            sport=comp.sport,
            matches=matches[: cfg.FORM_WINDOW],
            provenance=self._prov(
                ts, from_cache, f"{min(len(matches), cfg.FORM_WINDOW)} matchs {comp.label}"
            ),
        )


# --------------------------------------------------------------------------
# 6. API publique NHL — hockey
# --------------------------------------------------------------------------
class NhlApiProvider(BaseProvider):
    name = "nhl_api"
    label = "Données officielles NHL"
    supports = frozenset({"hockey"})
    BASE = "https://api-web.nhle.com/v1"

    def __init__(self, http: HttpClient):
        super().__init__(http)
        self.enabled = cfg.SOURCES.nhl_api

    def handles(self, comp: Competition) -> bool:
        return self.enabled and comp.nhl

    def _standings_rows(self, comp: Competition) -> tuple[list[dict], float, bool] | None:
        res = self.http.get_json(
            f"{self.BASE}/standings/now", ttl=cfg.TTL.standings, scope=comp.scope
        )
        if not res:
            return None
        rows = (res[0] or {}).get("standings") or []
        return (rows, res[1], res[2]) if rows else None

    @staticmethod
    def _row_name(row: dict) -> str:
        name = row.get("teamName")
        return (name or {}).get("default", "") if isinstance(name, dict) else str(name or "")

    @staticmethod
    def _row_abbrev(row: dict) -> str:
        abbrev = row.get("teamAbbrev")
        return (abbrev or {}).get("default", "") if isinstance(abbrev, dict) else str(abbrev or "")

    @staticmethod
    def _full_name(side: dict) -> str:
        """« Carolina » + « Hurricanes » → « Carolina Hurricanes ».

        Le calendrier officiel sépare ville et surnom, là où le classement
        donne le nom complet. Il faut les réunir, sinon les affiches ne se
        rapprochent pas des effectifs et le match reste introuvable.
        """
        def texte(champ: str) -> str:
            valeur = side.get(champ)
            if isinstance(valeur, dict):
                return str(valeur.get("default") or "").strip()
            return str(valeur or "").strip()

        complet = f"{texte('placeName')} {texte('commonName')}".strip()
        return complet or texte("abbrev")

    def fixtures(self, comp: Competition) -> list[Fixture]:
        """Calendrier officiel à venir. Gratuit, sans clé, sans quota."""
        if not self.handles(comp):
            return []
        res = self.http.get_json(
            f"{self.BASE}/schedule/now", ttl=cfg.TTL.events, scope=comp.scope
        )
        if not res:
            return []
        sorties: list[Fixture] = []
        for jour in (res[0] or {}).get("gameWeek") or []:
            for partie in jour.get("games") or []:
                # gameType 1 = matchs de préparation : ils ne comptent pas et
                # fausseraient l'analyse comme le pronostic.
                if partie.get("gameType") == 1:
                    continue
                domicile = self._full_name(partie.get("homeTeam") or {})
                exterieur = self._full_name(partie.get("awayTeam") or {})
                if not domicile or not exterieur:
                    continue
                sorties.append(Fixture(
                    home=domicile,
                    away=exterieur,
                    starts_at=_parse_dt(partie.get("startTimeUTC")),
                ))
        return sorties

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        got = self._standings_rows(comp)
        if not got:
            return None
        rows, ts, from_cache = got
        names = sorted({self._row_name(r) for r in rows if self._row_name(r)})
        return names, self._prov(ts, from_cache, f"{comp.label} · 32 franchises")

    def standings(self, comp: Competition) -> tuple[dict[str, Standing], Provenance] | None:
        if not self.handles(comp):
            return None
        got = self._standings_rows(comp)
        if not got:
            return None
        rows, ts, from_cache = got
        table: dict[str, Standing] = {}
        ordered = sorted(rows, key=lambda r: -int(r.get("points") or 0))
        for i, row in enumerate(ordered, start=1):
            name = self._row_name(row)
            if not name:
                continue
            table[name] = Standing(
                team=name,
                rank=i,
                played=int(row.get("gamesPlayed") or 0),
                points=int(row.get("points") or 0),
                goals_for=int(row.get("goalFor") or 0),
                goals_against=int(row.get("goalAgainst") or 0),
                won=int(row.get("wins") or 0),
                lost=int(row.get("losses") or 0),
                drawn=int(row.get("otLosses") or 0),
            )
        return (table, self._prov(ts, from_cache, f"{comp.label} · classement")) if table else None

    def form(self, comp: Competition, team: str) -> TeamForm | None:
        if not self.handles(comp):
            return None
        got = self._standings_rows(comp)
        if not got:
            return None
        rows = got[0]
        lookup = {self._row_name(r): self._row_abbrev(r) for r in rows if self._row_name(r)}
        chosen = best_match(team, lookup.keys())
        if not chosen:
            return None
        abbrev = lookup[chosen]

        res = self.http.get_json(
            f"{self.BASE}/club-schedule-season/{abbrev}/now",
            ttl=cfg.TTL.form,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res

        def finished(data: dict) -> list[dict]:
            return [
                g
                for g in ((data or {}).get("games") or [])
                if g.get("gameState") in {"OFF", "FINAL"}
            ]

        games = finished(payload)
        # Hors-saison, le calendrier courant n'a aucun match joué : on retombe
        # sur la saison précédente que l'API indique elle-même.
        if not games and (payload or {}).get("previousSeason"):
            prev = self.http.get_json(
                f"{self.BASE}/club-schedule-season/{abbrev}/{payload['previousSeason']}",
                ttl=cfg.TTL.form,
                scope=comp.scope,
            )
            if prev:
                payload, ts, from_cache = prev
                games = finished(payload)

        matches: list[MatchResult] = []
        for g in games:
            home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
            hs, aws = _to_float(home.get("score")), _to_float(away.get("score"))
            if hs is None or aws is None:
                continue
            is_home = home.get("abbrev") == abbrev
            opp_block = away if is_home else home
            opp = (opp_block.get("commonName") or {}).get("default") or opp_block.get("abbrev", "?")
            last_period = (g.get("gameOutcome") or {}).get("lastPeriodType")
            matches.append(
                MatchResult(
                    date=_parse_dt(g.get("gameDate")) or datetime.now(UTC),
                    opponent=opp,
                    home=is_home,
                    scored=hs if is_home else aws,
                    conceded=aws if is_home else hs,
                    competition=comp.label,
                    extra={"overtime": 1.0 if last_period in {"OT", "SO"} else 0.0},
                )
            )
        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        return TeamForm(
            team=chosen,
            sport=comp.sport,
            matches=matches[: cfg.FORM_WINDOW],
            provenance=self._prov(
                ts, from_cache, f"{min(len(matches), cfg.FORM_WINDOW)} matchs {comp.label}"
            ),
        )

    # -- confrontations directes ----------------------------------------
    def head_to_head(
        self, comp: Competition, home: str, away: str
    ) -> tuple[list[MatchResult], Provenance] | None:
        """Rencontres entre ces deux franchises sur les saisons disponibles.

        Le calendrier complet d'une saison est déjà téléchargé pour la forme :
        on le relit pour en extraire les confrontations, sans appel de plus.
        """
        if not self.handles(comp):
            return None
        got = self._standings_rows(comp)
        if not got:
            return None
        lookup = {self._row_name(r): self._row_abbrev(r) for r in got[0] if self._row_name(r)}
        home_key = best_match(home, lookup.keys())
        away_key = best_match(away, lookup.keys())
        if not home_key or not away_key:
            return None
        abbrev_home, abbrev_away = lookup[home_key], lookup[away_key]

        matches: list[MatchResult] = []
        newest, cached_all = 0.0, True
        season = "now"
        for _ in range(3):  # saison en cours puis les deux précédentes
            res = self.http.get_json(
                f"{self.BASE}/club-schedule-season/{abbrev_home}/{season}",
                ttl=cfg.TTL.form,
                scope=comp.scope,
            )
            if not res:
                break
            payload, ts, from_cache = res
            newest = max(newest, ts)
            cached_all = cached_all and from_cache
            for game in (payload or {}).get("games") or []:
                if game.get("gameState") not in {"OFF", "FINAL"}:
                    continue
                home_side, away_side = game.get("homeTeam") or {}, game.get("awayTeam") or {}
                sides = {home_side.get("abbrev"), away_side.get("abbrev")}
                if sides != {abbrev_home, abbrev_away}:
                    continue
                hs, aws = _to_float(home_side.get("score")), _to_float(away_side.get("score"))
                if hs is None or aws is None:
                    continue
                is_home = home_side.get("abbrev") == abbrev_home
                matches.append(
                    MatchResult(
                        date=_parse_dt(game.get("gameDate")) or datetime.now(UTC),
                        opponent=away_key,
                        home=is_home,
                        scored=hs if is_home else aws,
                        conceded=aws if is_home else hs,
                        competition=comp.label,
                    )
                )
            season = (payload or {}).get("previousSeason")
            if not season:
                break

        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        return matches, self._prov(
            newest or time.time(), cached_all, f"{len(matches)} confrontation(s)"
        )


# --------------------------------------------------------------------------
# 7. Actualités (flux RSS, sans clé) — affichage minimal uniquement
# --------------------------------------------------------------------------
_INJURY_WORDS = (
    "blessure", "blessé", "forfait", "absent", "absence", "suspendu", "suspension",
    "injury", "injured", "out for", "doubtful", "ruled out", "sidelined",
)


class NewsRssProvider(BaseProvider):
    name = "news_rss"
    label = "Actualité sportive"
    supports = frozenset({"football", "basket", "tennis", "hockey"})
    BASE = "https://news.google.com/rss/search"

    def __init__(self, http: HttpClient):
        super().__init__(http)
        self.enabled = cfg.SOURCES.news_rss

    def handles(self, comp: Competition) -> bool:
        return self.enabled

    def news(self, comp: Competition, team: str) -> list[NewsFlag]:
        if not self.enabled or not team:
            return []
        res = self.http.get_json(
            self.BASE,
            params={
                "q": f"{team} blessure OR forfait OR absent",
                "hl": "fr", "gl": "FR", "ceid": "FR:fr",
            },
            ttl=cfg.TTL.news,
            expect="text",
            scope=comp.scope,
        )
        if not res:
            return []
        payload, ts, from_cache = res
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            return []
        prov = self._prov(ts, from_cache, "flux RSS")
        flags: list[NewsFlag] = []
        for item in root.iterfind(".//item"):
            title = (item.findtext("title") or "").strip()
            if not any(w in title.lower() for w in _INJURY_WORDS):
                continue
            flags.append(
                NewsFlag(
                    team=team,
                    headline=title,
                    url=(item.findtext("link") or "").strip(),
                    published=_parse_dt(item.findtext("pubDate")),
                    provenance=prov,
                )
            )
            if len(flags) >= 2:
                break
        return flags


# --------------------------------------------------------------------------
# 8. Météo (Open-Meteo, gratuit sans clé) — contexte affiché uniquement
# --------------------------------------------------------------------------
class WeatherProvider(BaseProvider):
    """Bulletin météo au coup d'envoi.

    Volontairement NON injecté dans le modèle : aucun coefficient météo
    honnêtement calibré n'est disponible ici. La donnée est affichée comme
    contexte, à charge de l'utilisateur d'en tenir compte.
    """

    name = "open_meteo"
    label = "Prévisions météo"
    supports = frozenset({"football"})
    GEO = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, http: HttpClient):
        super().__init__(http)
        self.enabled = cfg.SOURCES.weather

    def handles(self, comp: Competition) -> bool:
        return self.enabled and comp.sport == "football"

    def forecast(self, comp: Competition, place: str, when: datetime | None) -> WeatherInfo | None:
        if not self.handles(comp) or not place or when is None:
            return None
        delta_days = (when - datetime.now(UTC)).total_seconds() / 86400
        if not -1 <= delta_days <= 15:
            return None

        geo = self.http.get_json(
            self.GEO,
            params={"name": place.split(",")[0].strip(), "count": 1, "language": "fr"},
            ttl=30 * 24 * 3600,
            scope=comp.scope,
        )
        if not geo:
            return None
        results = (geo[0] or {}).get("results") or []
        if not results:
            return None
        lat, lon = results[0].get("latitude"), results[0].get("longitude")
        city = results[0].get("name", place)

        res = self.http.get_json(
            self.FORECAST,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m,precipitation,wind_speed_10m",
                "forecast_days": 16, "timezone": "UTC",
            },
            ttl=cfg.TTL.weather,
            scope=comp.scope,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        hourly = (payload or {}).get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            return None
        target = when.strftime("%Y-%m-%dT%H:00")
        idx = min(
            range(len(times)),
            key=lambda i: abs(
                (_parse_dt(times[i]) or datetime.now(UTC)) - when
            ).total_seconds(),
        )
        if abs((_parse_dt(times[idx]) or datetime.now(UTC)) - when).total_seconds() > 6 * 3600:
            return None

        def at(series: str) -> float | None:
            values = hourly.get(series) or []
            return _to_float(values[idx]) if idx < len(values) else None

        return WeatherInfo(
            place=city,
            temperature_c=at("temperature_2m"),
            precipitation_mm=at("precipitation"),
            wind_kmh=at("wind_speed_10m"),
            provenance=self._prov(ts, from_cache, f"prévision {target} UTC"),
        )


# --------------------------------------------------------------------------
# 9. openfootball — calendriers officiels, données du domaine public
# --------------------------------------------------------------------------
class OpenFootballProvider(BaseProvider):
    """Calendriers complets des grands championnats (openfootball, GitHub).

    Jeu de données public et librement réutilisable. Il donne l'effectif
    **complet** d'une saison — là où les paliers gratuits des API bridées
    plafonnent à une dizaine d'équipes.
    """

    name = "openfootball"
    label = "Calendriers officiels des championnats"
    supports = frozenset({"football"})
    BASE = "https://raw.githubusercontent.com/openfootball/football.json/master"

    def __init__(self, http: HttpClient):
        super().__init__(http)
        self.enabled = cfg.SOURCES.openfootball

    def handles(self, comp: Competition) -> bool:
        return self.enabled and bool(comp.openfootball_code)

    def _season_file(self, comp: Competition) -> tuple[dict, float, bool, int] | None:
        """Essaie la saison à venir, puis celle en cours (publication décalée)."""
        current = cfg.european_season()
        for year in (current, current - 1):
            res = self.http.get_json(
                f"{self.BASE}/{cfg.openfootball_season(year)}/{comp.openfootball_code}.json",
                ttl=cfg.TTL.roster,
                scope=comp.scope,
            )
            if res and (res[0] or {}).get("matches"):
                return res[0], res[1], res[2], year
        return None

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        got = self._season_file(comp)
        if not got:
            return None
        payload, ts, from_cache, year = got
        names: set[str] = set()
        for match in payload.get("matches") or []:
            for side in ("team1", "team2"):
                value = match.get(side)
                if isinstance(value, dict):
                    value = value.get("name")
                if value:
                    names.add(str(value).strip())
        if not names:
            return None
        detail = f"{comp.label} {cfg.openfootball_season(year)} ({len(names)} équipes)"
        return sorted(names), self._prov(ts, from_cache, detail, season=year)

    @staticmethod
    def _full_time_score(match: dict) -> tuple[float, float] | None:
        """Score final, quelle que soit la forme du champ.

        openfootball écrit tantôt `{"ft": [4, 2], "ht": [1, 0]}`, tantôt
        directement `[0, 0]` — les deux existent dans le même fichier. Un
        match non joué n'a pas de score du tout : il renvoie `None` et n'est
        jamais compté comme un 0-0.
        """
        score = match.get("score")
        if isinstance(score, dict):
            score = score.get("ft")
        if not isinstance(score, (list, tuple)) or len(score) < 2:
            return None
        first, second = _to_float(score[0]), _to_float(score[1])
        if first is None or second is None:
            return None
        return first, second

    # -- classement ------------------------------------------------------
    def standings(self, comp: Competition) -> tuple[dict[str, Standing], Provenance] | None:
        """Classement reconstruit à partir des résultats de la saison.

        Les deux fournisseurs de classement du projet exigent une clé
        (football-data.org, API-Football). Sans elles, aucun classement
        n'existait — alors que le fichier de saison openfootball, déjà
        téléchargé et mis en cache pour l'effectif et la forme, contient
        tous les résultats nécessaires. On le calcule donc ici : aucune
        requête supplémentaire, aucun quota consommé.

        Une coupe n'a pas de classement : on s'abstient, comme les autres
        fournisseurs. En début de saison, tant qu'aucun match n'est joué,
        on renvoie `None` plutôt qu'un tableau de zéros qui donnerait
        l'illusion d'une information.
        """
        if not self.handles(comp) or comp.is_cup:
            return None
        got = self._season_file(comp)
        if not got:
            return None
        payload, ts, from_cache, year = got

        cumul: dict[str, dict[str, int]] = {}

        def ligne(nom: str) -> dict[str, int]:
            return cumul.setdefault(
                nom,
                {"played": 0, "points": 0, "gf": 0, "ga": 0, "won": 0, "drawn": 0, "lost": 0},
            )

        for match in payload.get("matches") or []:
            score = self._full_time_score(match)
            if score is None:
                continue          # match non joué : il ne compte pas
            noms = []
            for side in ("team1", "team2"):
                valeur = match.get(side)
                if isinstance(valeur, dict):
                    valeur = valeur.get("name")
                noms.append(str(valeur).strip() if valeur else "")
            if not all(noms):
                continue
            domicile, exterieur = ligne(noms[0]), ligne(noms[1])
            buts_dom, buts_ext = int(score[0]), int(score[1])

            for equipe, pour, contre in (
                (domicile, buts_dom, buts_ext),
                (exterieur, buts_ext, buts_dom),
            ):
                equipe["played"] += 1
                equipe["gf"] += pour
                equipe["ga"] += contre
                if pour > contre:
                    equipe["won"] += 1
                    equipe["points"] += 3
                elif pour == contre:
                    equipe["drawn"] += 1
                    equipe["points"] += 1
                else:
                    equipe["lost"] += 1

        if not cumul or not any(v["played"] for v in cumul.values()):
            return None

        classe = sorted(
            cumul.items(),
            key=lambda kv: (-kv[1]["points"], -(kv[1]["gf"] - kv[1]["ga"]), -kv[1]["gf"], kv[0]),
        )
        table = {
            nom: Standing(
                team=nom,
                rank=rang,
                played=v["played"],
                points=v["points"],
                goals_for=v["gf"],
                goals_against=v["ga"],
                won=v["won"],
                drawn=v["drawn"],
                lost=v["lost"],
            )
            for rang, (nom, v) in enumerate(classe, start=1)
        }
        joues = sum(v["played"] for v in cumul.values()) // 2
        detail = f"{comp.label} {cfg.openfootball_season(year)} — calculé sur {joues} matchs joués"
        return table, self._prov(ts, from_cache, detail, season=year)

    # -- forme récente ---------------------------------------------------
    def form(self, comp: Competition, team: str) -> TeamForm | None:
        """Derniers matchs joués, extraits du calendrier officiel.

        Les fichiers openfootball contiennent **tous** les résultats de la
        saison. C'est la seule source d'historique profond disponible sans
        clé : les paliers gratuits des API bridées plafonnent à un match, ce
        qui ne permet aucune estimation sérieuse.
        """
        if not self.handles(comp):
            return None

        current = cfg.european_season()
        matches: list[MatchResult] = []
        newest, cached_all, season_used = 0.0, True, None

        # On remonte les saisons jusqu'à disposer d'un historique exploitable.
        for year in (current, current - 1, current - 2):
            res = self.http.get_json(
                f"{self.BASE}/{cfg.openfootball_season(year)}/{comp.openfootball_code}.json",
                ttl=cfg.TTL.form,
                scope=comp.scope,
            )
            if not res:
                continue
            payload, ts, from_cache = res
            found = self._extract_team_matches(payload, team, comp.label)
            if not found:
                continue
            newest = max(newest, ts)
            cached_all = cached_all and from_cache
            season_used = season_used if season_used is not None else year
            matches.extend(found)
            if len(matches) >= cfg.FORM_WINDOW:
                break

        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        matches = matches[: cfg.FORM_WINDOW]
        detail = (
            f"{len(matches)} matchs joués · "
            f"{cfg.openfootball_season(season_used)}"
        )
        return TeamForm(
            team=team,
            sport=comp.sport,
            matches=matches,
            provenance=self._prov(newest or time.time(), cached_all, detail,
                                  season=season_used),
        )

    @staticmethod
    def _extract_team_matches(payload: dict, team: str, label: str) -> list[MatchResult]:
        """Matchs terminés d'une équipe, vus de son point de vue."""
        out: list[MatchResult] = []
        for match in (payload or {}).get("matches") or []:
            t1, t2 = match.get("team1"), match.get("team2")
            if isinstance(t1, dict):
                t1 = t1.get("name")
            if isinstance(t2, dict):
                t2 = t2.get("name")
            if not t1 or not t2:
                continue
            is_home = name_similarity(t1, team) >= 0.75
            is_away = name_similarity(t2, team) >= 0.75
            if not (is_home or is_away):
                continue
            goals = OpenFootballProvider._full_time_score(match)
            played = _parse_dt(match.get("date"))
            if goals is None or played is None:
                continue  # match non joué : jamais compté comme un 0-0
            g1, g2 = goals
            out.append(
                MatchResult(
                    date=played,
                    opponent=t2 if is_home else t1,
                    home=is_home,
                    scored=g1 if is_home else g2,
                    conceded=g2 if is_home else g1,
                    competition=label,
                )
            )
        return out

    # -- moyenne réelle de la compétition --------------------------------
    def league_average(self, comp: Competition) -> tuple[float, int, Provenance] | None:
        """Buts par équipe et par match, calculés sur la saison entière.

        Évite de retomber sur une constante : chaque championnat a son propre
        volume de buts, et il est directement mesurable ici.
        """
        if not self.handles(comp):
            return None
        got = self._season_file(comp)
        if not got:
            return None
        payload, ts, from_cache, _year = got
        goals, played = 0.0, 0
        for match in payload.get("matches") or []:
            scored = self._full_time_score(match)
            if scored is None:
                continue
            goals += scored[0] + scored[1]
            played += 1
        if played < 20:
            return None
        average = goals / (2 * played)
        return average, played, self._prov(
            ts, from_cache, f"{played} matchs de la saison"
        )

    # -- confrontations directes ----------------------------------------
    HISTORY_SEASONS = 5

    def head_to_head(
        self, comp: Competition, home: str, away: str
    ) -> tuple[list[MatchResult], Provenance] | None:
        """Toutes les rencontres entre ces deux équipes, sur plusieurs saisons.

        Les calendriers openfootball contiennent les scores : il suffit de les
        parcourir. C'est la seule source d'historique de confrontations
        réellement gratuite et complète pour les grands championnats.
        """
        if not self.handles(comp):
            return None
        current = cfg.european_season()
        matches: list[MatchResult] = []
        newest, cached_all = 0.0, True

        for year in range(current, current - self.HISTORY_SEASONS, -1):
            res = self.http.get_json(
                f"{self.BASE}/{cfg.openfootball_season(year)}/{comp.openfootball_code}.json",
                ttl=cfg.TTL.roster,
                scope=comp.scope,
            )
            if not res:
                continue
            payload, ts, from_cache = res
            newest = max(newest, ts)
            cached_all = cached_all and from_cache
            matches.extend(self._extract_h2h(payload, home, away, comp.label))

        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        detail = f"{len(matches)} rencontre(s) sur {self.HISTORY_SEASONS} saisons"
        return matches, self._prov(newest or time.time(), cached_all, detail)

    # Seuil volontairement élevé : deux clubs d'une même ville partagent un
    # mot ("Manchester United" / "Manchester City") et franchiraient un seuil
    # ordinaire. Une confrontation attribuée au mauvais club fausserait tout
    # l'historique, donc on préfère en manquer une que d'en inventer une.
    H2H_NAME_THRESHOLD = 0.88

    @classmethod
    def _extract_h2h(cls, payload: dict, home: str, away: str, label: str) -> list[MatchResult]:
        out: list[MatchResult] = []
        threshold = cls.H2H_NAME_THRESHOLD
        for match in (payload or {}).get("matches") or []:
            t1, t2 = match.get("team1"), match.get("team2")
            if isinstance(t1, dict):
                t1 = t1.get("name")
            if isinstance(t2, dict):
                t2 = t2.get("name")
            if not t1 or not t2:
                continue
            direct = (
                name_similarity(t1, home) >= threshold
                and name_similarity(t2, away) >= threshold
            )
            swapped = (
                name_similarity(t1, away) >= threshold
                and name_similarity(t2, home) >= threshold
            )
            if not (direct or swapped):
                continue
            goals = OpenFootballProvider._full_time_score(match)
            played = _parse_dt(match.get("date"))
            if goals is None or played is None:
                continue
            g1, g2 = goals
            # Point de vue de l'équipe « domicile » de la sélection courante.
            out.append(
                MatchResult(
                    date=played,
                    opponent=away,
                    home=direct,
                    scored=g1 if direct else g2,
                    conceded=g2 if direct else g1,
                    competition=label,
                )
            )
        return out


# --------------------------------------------------------------------------
# 9 bis. football-data.co.uk — archives libres : stats détaillées ET cotes
# --------------------------------------------------------------------------
# Les fichiers de cette archive nomment les clubs en abrégé. Un rapprochement
# purement littéral échoue (« Man City » n'est pas assez proche de
# « Manchester City » pour être sûr), et une règle floue confondrait des clubs
# distincts. Une table explicite est le seul moyen fiable.
FOOTBALLDATA_ALIASES = {
    # Angleterre
    "man city": "Manchester City", "man united": "Manchester United",
    "nott'm forest": "Nottingham Forest", "newcastle": "Newcastle United",
    "wolves": "Wolverhampton Wanderers", "tottenham": "Tottenham Hotspur",
    "west ham": "West Ham United", "leeds": "Leeds United",
    "brighton": "Brighton & Hove Albion", "leicester": "Leicester City",
    "sheffield united": "Sheffield United", "west brom": "West Bromwich Albion",
    "norwich": "Norwich City", "hull": "Hull City", "stoke": "Stoke City",
    "cardiff": "Cardiff City", "swansea": "Swansea City", "coventry": "Coventry City",
    "ipswich": "Ipswich Town", "luton": "Luton Town",
    # Espagne
    "ath madrid": "Atlético Madrid", "ath bilbao": "Athletic Bilbao",
    "espanol": "Espanyol", "sociedad": "Real Sociedad", "betis": "Real Betis",
    "vallecano": "Rayo Vallecano", "celta": "Celta Vigo", "alaves": "Alavés",
    "la coruna": "Deportivo de A Coruña",
    # Allemagne
    "bayern munich": "Bayern Munich", "ein frankfurt": "Eintracht Frankfurt",
    "m'gladbach": "Borussia Mönchengladbach", "dortmund": "Borussia Dortmund",
    "leverkusen": "Bayer Leverkusen", "hoffenheim": "TSG Hoffenheim",
    "stuttgart": "VfB Stuttgart", "wolfsburg": "VfL Wolfsburg",
    "werder bremen": "Werder Bremen", "union berlin": "Union Berlin",
    "mainz": "Mainz 05", "freiburg": "SC Freiburg", "augsburg": "FC Augsburg",
    "heidenheim": "1. FC Heidenheim", "st pauli": "FC St. Pauli",
    "hamburg": "Hamburger SV", "rb leipzig": "RB Leipzig", "fc koln": "1. FC Köln",
    # Italie
    "inter": "Inter Milan", "milan": "AC Milan", "roma": "Roma", "napoli": "Napoli",
    "juventus": "Juventus", "lazio": "Lazio", "verona": "Hellas Verona",
    # France
    "paris sg": "Paris SG", "marseille": "Marseille", "st etienne": "Saint-Étienne",
    "paris fc": "Paris FC",
}


class FootballDataUkProvider(BaseProvider):
    """Archives CSV libres : résultats, statistiques détaillées et cotes.

    C'est la seule source **sans clé** qui fournit à la fois les corners, les
    tirs, les cartons et les cotes de clôture de plusieurs bookmakers.

    ⚠️ Ces cotes concernent des matchs **déjà joués**. Elles ne sont jamais
    présentées comme les cotes d'une rencontre à venir : elles servent à
    mesurer la valeur que le marché accordait à chaque équipe, ce qui donne
    un repère au modèle quand aucune cote en direct n'est disponible.
    """

    name = "football_data_uk"
    label = "Archives de résultats et de cotes"
    supports = frozenset({"football"})
    BASE = "https://www.football-data.co.uk/mmz4281"

    def __init__(self, http: HttpClient):
        super().__init__(http)
        self.enabled = cfg.SOURCES.footballdata_uk

    def handles(self, comp: Competition) -> bool:
        return self.enabled and bool(comp.footballdata_code)

    @staticmethod
    def _season_code(year: int) -> str:
        """« 2025 » → « 2526 », le format de nommage de l'archive."""
        return f"{str(year)[2:]}{str(year + 1)[2:]}"

    def _rows(self, comp: Competition) -> tuple[list[dict], float, bool, int] | None:
        """Matchs de la saison la plus récente disponible."""
        current = cfg.european_season()
        for year in (current, current - 1, current - 2):
            res = self.http.get_json(
                f"{self.BASE}/{self._season_code(year)}/{comp.footballdata_code}.csv",
                ttl=cfg.TTL.form,
                scope=comp.scope,
                expect="text",
            )
            if not res:
                continue
            payload, ts, from_cache = res
            rows = self._parse_csv(payload)
            if rows:
                return rows, ts, from_cache, year
        return None

    @staticmethod
    def _parse_csv(text: str) -> list[dict]:
        import csv as _csv
        import io

        try:
            # Le fichier commence par une marque d'octets : utf-8-sig la retire.
            reader = _csv.DictReader(io.StringIO(text.lstrip("﻿")))
            return [row for row in reader if row.get("HomeTeam")]
        except Exception:
            return []

    def _canonical(self, raw: str) -> str:
        return FOOTBALLDATA_ALIASES.get((raw or "").strip().lower(), (raw or "").strip())

    def _same_team(self, raw: str, team: str) -> bool:
        return name_similarity(self._canonical(raw), team) >= 0.75

    @staticmethod
    def _stat(row: dict, key: str) -> float | None:
        return _to_float(row.get(key))

    def _match_from_row(self, row: dict, is_home: bool, label: str) -> MatchResult | None:
        hg, ag = self._stat(row, "FTHG"), self._stat(row, "FTAG")
        played = _parse_dt(row.get("Date"))
        if hg is None or ag is None or played is None:
            return None

        # Statistiques détaillées, du point de vue de l'équipe analysée.
        prefix, other = ("H", "A") if is_home else ("A", "H")
        extra: dict[str, float] = {}
        for field_name, column in (
            ("corners_for", "C"), ("shots", "S"), ("shots_on_target", "ST"),
            ("yellow_cards", "Y"), ("red_cards", "R"),
        ):
            value = self._stat(row, f"{prefix}{column}")
            if value is not None:
                extra[field_name] = value
        against = self._stat(row, f"{other}C")
        if against is not None:
            extra["corners_against"] = against
            if "corners_for" in extra:
                extra["corners_total"] = extra["corners_for"] + against

        return MatchResult(
            date=played,
            opponent=self._canonical(row.get("AwayTeam") if is_home else row.get("HomeTeam")),
            home=is_home,
            scored=hg if is_home else ag,
            conceded=ag if is_home else hg,
            competition=label,
            extra=extra,
        )

    # -- forme récente, avec statistiques détaillées ---------------------
    def form(self, comp: Competition, team: str) -> TeamForm | None:
        if not self.handles(comp):
            return None
        got = self._rows(comp)
        if not got:
            return None
        rows, ts, from_cache, year = got

        matches: list[MatchResult] = []
        for row in rows:
            if self._same_team(row.get("HomeTeam"), team):
                built = self._match_from_row(row, True, comp.label)
            elif self._same_team(row.get("AwayTeam"), team):
                built = self._match_from_row(row, False, comp.label)
            else:
                continue
            if built is not None:
                matches.append(built)

        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        matches = matches[: cfg.FORM_WINDOW]
        return TeamForm(
            team=team,
            sport=comp.sport,
            matches=matches,
            provenance=self._prov(
                ts, from_cache,
                f"{len(matches)} matchs détaillés · {cfg.openfootball_season(year)}",
                season=year,
            ),
        )

    def head_to_head(
        self, comp: Competition, home: str, away: str
    ) -> tuple[list[MatchResult], Provenance] | None:
        if not self.handles(comp):
            return None
        got = self._rows(comp)
        if not got:
            return None
        rows, ts, from_cache, _year = got
        matches: list[MatchResult] = []
        for row in rows:
            direct = self._same_team(row.get("HomeTeam"), home) and \
                self._same_team(row.get("AwayTeam"), away)
            swapped = self._same_team(row.get("HomeTeam"), away) and \
                self._same_team(row.get("AwayTeam"), home)
            if not (direct or swapped):
                continue
            built = self._match_from_row(row, direct, comp.label)
            if built is not None:
                built.opponent = away
                matches.append(built)
        if not matches:
            return None
        matches.sort(key=lambda m: m.date, reverse=True)
        return matches, self._prov(ts, from_cache, f"{len(matches)} confrontation(s)")

    # -- repère de marché dérivé des cotes de clôture --------------------
    # Colonnes de cotes, par ordre de préférence : la moyenne du marché
    # d'abord, puis des bookmakers de référence.
    ODDS_COLUMNS = (("AvgH", "AvgD", "AvgA"), ("B365H", "B365D", "B365A"),
                    ("PSH", "PSD", "PSA"), ("MaxH", "MaxD", "MaxA"))

    def market_ratings(
        self, comp: Competition
    ) -> tuple[dict[str, dict], Provenance] | None:
        """Profil de marché de chaque équipe, à domicile et à l'extérieur.

        Pour chaque match de la saison, les cotes de clôture sont converties
        en probabilités sans marge. On en tire, par équipe, la probabilité
        moyenne de gagner / faire nul / perdre — séparément selon le lieu.

        C'est un **repère de marché**, pas la cote d'une rencontre à venir.
        La distinction est maintenue jusque dans l'interface : le moteur s'en
        sert comme ancrage quand aucune cote en direct n'est disponible, et
        l'affiche comme tel.
        """
        if not self.handles(comp):
            return None
        got = self._rows(comp)
        if not got:
            return None
        rows, ts, from_cache, _year = got

        buckets: dict[str, dict[str, list[tuple[float, float, float]]]] = {}
        counted = 0
        for row in rows:
            prices = self._row_odds(row)
            if prices is None:
                continue
            counted += 1
            home_p, draw_p, away_p = prices
            home = self._canonical(row.get("HomeTeam"))
            away = self._canonical(row.get("AwayTeam"))
            buckets.setdefault(home, {}).setdefault("home", []).append(
                (home_p, draw_p, away_p)
            )
            # Vu de l'équipe visiteuse, victoire et défaite s'inversent.
            buckets.setdefault(away, {}).setdefault("away", []).append(
                (away_p, draw_p, home_p)
            )

        profiles: dict[str, dict] = {}
        for team, sides in buckets.items():
            entry: dict[str, Any] = {"n": 0}
            for side, samples in sides.items():
                if len(samples) < 3:
                    continue
                entry[side] = tuple(
                    sum(sample[i] for sample in samples) / len(samples) for i in range(3)
                )
                entry["n"] += len(samples)
            if "home" in entry or "away" in entry:
                profiles[team] = entry

        if len(profiles) < 6:
            return None
        return profiles, self._prov(
            ts, from_cache, f"cotes de clôture de {counted} matchs"
        )

    def _row_odds(self, row: dict) -> tuple[float, float, float] | None:
        """Probabilités sans marge d'un match, à partir de ses cotes."""
        for home_col, draw_col, away_col in self.ODDS_COLUMNS:
            odds = [self._stat(row, c) for c in (home_col, draw_col, away_col)]
            if any(o is None or o <= 1.0 for o in odds):
                continue
            implied = [1.0 / o for o in odds]
            total = sum(implied)
            if not 0.98 <= total <= 1.40:
                continue
            return tuple(value / total for value in implied)  # type: ignore[return-value]
        return None


# --------------------------------------------------------------------------
# 10. Wikipédia — effectif exact de la saison en cours (API MediaWiki)
# --------------------------------------------------------------------------
class WikipediaProvider(BaseProvider):
    """Effectifs lus sur la page de saison via l'API publique MediaWiki.

    Les articles de saison contiennent une table de classement dont chaque
    équipe est déclarée par `name_XXX = [[Club]]`. C'est la source la plus à
    jour pour savoir *qui joue cette saison* (promus et relégués compris).
    """

    name = "wikipedia"
    label = "Encyclopédie sportive"
    supports = frozenset({"football", "basket", "hockey"})
    BASE = "https://en.wikipedia.org/w/api.php"

    # Un champ vaut soit un lien wiki complet (qui peut contenir un « | »
    # interne, d'où la première alternative), soit un texte simple qui
    # s'arrête au séparateur suivant.
    _TEAM_FIELD = re.compile(
        r"\|\s*name_[A-Za-z0-9]{2,6}\s*=\s*(\[\[[^\]]+\]\]|[^\n|}]+)"
    )
    _WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

    def __init__(self, http: HttpClient):
        super().__init__(http)
        self.enabled = cfg.SOURCES.wikipedia

    def handles(self, comp: Competition) -> bool:
        return self.enabled and bool(comp.wikipedia_page)

    @classmethod
    def _clean(cls, raw: str) -> str:
        """« [[Arsenal F.C.|Arsenal]] » → « Arsenal »."""
        raw = raw.strip()
        link = cls._WIKILINK.search(raw)
        if link:
            return (link.group(2) or link.group(1)).strip()
        return raw.split("<")[0].split("{{")[0].strip(" []|")

    def _wikitext(self, title: str, scope: str) -> tuple[str, float, bool] | None:
        res = self.http.get_json(
            self.BASE,
            params={
                "action": "query", "format": "json", "prop": "revisions",
                "titles": title, "rvprop": "content", "rvslots": "main",
                "redirects": 1,
            },
            ttl=cfg.TTL.roster,
            scope=scope,
        )
        if not res:
            return None
        pages = ((res[0] or {}).get("query") or {}).get("pages") or {}
        for page in pages.values():
            revisions = page.get("revisions") or []
            if revisions:
                content = revisions[0].get("slots", {}).get("main", {}).get("*", "")
                if content:
                    return content, res[1], res[2]
        return None

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        # Saison en cours, puis précédente si la page n'existe pas encore.
        current = cfg.european_season()
        for year in (current, current - 1):
            title = comp.wikipedia_title(year)
            got = self._wikitext(title, comp.scope)
            if not got:
                continue
            content, ts, from_cache = got
            names = self._extract_table(content, comp.expected_teams)
            if len(names) >= 4:
                return sorted(names), self._prov(
                    ts, from_cache, f"{title} ({len(names)} équipes)", season=year
                )
        return None

    @classmethod
    def _extract_table(cls, content: str, expected: int = 0) -> list[str]:
        """Lit le tableau du championnat, et lui seul.

        Une page de saison contient souvent plusieurs tableaux (montées et
        descentes, barrages…). On retient donc celui dont l'effectif
        correspond au nombre d'équipes attendu ; à défaut, le premier
        tableau exploitable — jamais l'union de tous, qui mélangerait des
        clubs d'autres divisions.
        """
        blocks = re.split(r"\{\{\s*#invoke:\s*sports table", content, flags=re.I)
        candidates: list[list[str]] = []
        for block in blocks[1:] or [content]:
            names = {cls._clean(raw) for raw in cls._TEAM_FIELD.findall(block)}
            names = {n for n in names if n and 2 < len(n) < 60}
            if len(names) >= 3:
                candidates.append(sorted(names))
        if not candidates:
            names = {cls._clean(raw) for raw in cls._TEAM_FIELD.findall(content)}
            return sorted(n for n in names if n and 2 < len(n) < 60)
        if expected:
            exact = [c for c in candidates if len(c) == expected]
            if exact:
                return exact[0]
        return candidates[0]


# --------------------------------------------------------------------------
# 11. Wikidata — franchises d'une ligue (service SPARQL public)
# --------------------------------------------------------------------------
class WikidataProvider(BaseProvider):
    """Liste des clubs/franchises rattachés à une ligue.

    Utile surtout pour les ligues fermées (NBA, WNBA) où l'effectif ne change
    pas d'une saison à l'autre. Pour les championnats à promotion/relégation,
    la source est moins précise : elle sert de filet de sécurité.
    """

    name = "wikidata"
    label = "Base de connaissances sportive"
    supports = frozenset({"football", "basket", "hockey"})
    BASE = "https://query.wikidata.org/sparql"

    # Requête volontairement simple : une contrainte de type (« est une
    # équipe sportive ») écarte joueurs et saisons, et coûte bien moins cher
    # qu'un filtre par négation, qui faisait expirer le service public.
    QUERY = """
    SELECT DISTINCT ?tLabel WHERE {
      ?t wdt:P118 wd:%s .
      ?t wdt:P31/wdt:P279* wd:Q12973014 .
      FILTER NOT EXISTS { ?t wdt:P576 ?dissolved }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en,fr". }
    }
    """

    def __init__(self, http: HttpClient):
        super().__init__(http)
        self.enabled = cfg.SOURCES.wikidata

    def handles(self, comp: Competition) -> bool:
        # Source de complément : inutile — et coûteuse en temps — dès qu'une
        # source datée (calendrier officiel, page de saison) couvre déjà la
        # compétition, car elle seule peut faire autorité sur l'effectif.
        if not self.enabled or not comp.wikidata_qid:
            return False
        return not (comp.wikipedia_page or comp.openfootball_code or comp.nhl)

    def participants(self, comp: Competition) -> tuple[list[str], Provenance] | None:
        if not self.handles(comp):
            return None
        res = self.http.get_json(
            self.BASE,
            params={"query": self.QUERY % comp.wikidata_qid, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
            ttl=cfg.TTL.roster,
            scope=comp.scope,
            # Le service SPARQL public est lent : le délai standard ne suffit
            # pas. L'appel étant parallèle et mis en cache une semaine, ce
            # supplément ne coûte rien à l'usage.
            timeout=cfg.SPARQL_TIMEOUT,
        )
        if not res:
            return None
        payload, ts, from_cache = res
        rows = ((payload or {}).get("results") or {}).get("bindings") or []
        names = {
            row["tLabel"]["value"].strip()
            for row in rows
            if row.get("tLabel", {}).get("value")
        }
        # Les libellés non traduits ressemblent à « Q12345 » : on les écarte.
        names = {n for n in names if not re.fullmatch(r"Q\d+", n) and len(n) > 2}
        if len(names) < 4:
            return None
        return sorted(names), self._prov(
            ts, from_cache, f"{comp.label} ({len(names)} clubs référencés)"
        )


# ==========================================================================
# Agrégateur : ordre de priorité + repli, tout piloté par compétition
# ==========================================================================
class DataHub:
    """Point d'entrée unique de la couche données.

    L'ordre des providers vaut priorité : le premier qui répond gagne, les
    suivants servent de repli. Brancher une source payante = l'insérer en tête
    de `self.providers`. Aucune modification du moteur n'est nécessaire.
    """

    def __init__(self, keys: cfg.ApiKeys | None = None):
        keys = keys or cfg.KEYS
        self.cache = CacheStore()
        self.quota = QuotaTracker()
        self.http = HttpClient(self.cache, self.quota)
        self.odds_history = OddsHistory()

        self.odds_provider = TheOddsApiProvider(self.http, keys.odds_api, self.odds_history)
        self.weather_provider = WeatherProvider(self.http)
        self.sportsdb = TheSportsDbProvider(self.http, keys.thesportsdb)
        # L'ordre vaut priorité en cas d'égalité de fiabilité.
        self.providers: list[BaseProvider] = [
            self.odds_provider,
            ApiFootballProvider(self.http, keys.rapidapi),
            FootballDataProvider(self.http, keys.football_data),
            BallDontLieProvider(self.http, keys.balldontlie),
            NhlApiProvider(self.http),
            FootballDataUkProvider(self.http),
            OpenFootballProvider(self.http),
            WikipediaProvider(self.http),
            WikidataProvider(self.http),
            self.sportsdb,
            # Complément statistique seul : il ne fournit pas de forme et
            # n'entre donc en concurrence avec aucune source ci-dessus.
            SportApi7Provider(self.http, keys.rapidapi),
            NewsRssProvider(self.http),
        ]
        self._research = None

    @property
    def research(self):
        """Moteur de recherche approfondie (import différé : évite un cycle)."""
        if self._research is None:
            from research import DeepResearch

            self._research = DeepResearch(self)
        return self._research

    # -- introspection ---------------------------------------------------
    def competitions(self, sport: str) -> list[Competition]:
        return cfg.competitions(sport)

    def sources_for(self, comp: Competition) -> list[BaseProvider]:
        out = []
        for p in self.providers:
            try:
                if p.enabled and p.handles(comp):
                    out.append(p)
            except Exception:
                continue
        return out

    def missing_keys(self) -> list[str]:
        return [p.label for p in self.providers if not p.enabled]

    def quota_status(self) -> list[QuotaStatus]:
        return self.quota.all_status()

    def cache_scopes(self) -> dict[str, int]:
        return self.cache.scopes()

    def clear_competition_cache(self, comp: Competition) -> int:
        return self.cache.clear_scope(comp.scope)

    # -- menus déroulants -------------------------------------------------
    def participants(self, comp: Competition) -> tuple[list[str], list[Provenance]]:
        """Effectif complet de CETTE compétition, fusionné entre toutes les sources."""
        result = self.research.roster(comp)
        return result.names, result.sources

    def roster(self, comp: Competition):
        """Version détaillée : noms + couverture + fiabilité."""
        return self.research.roster(comp)

    def final_score(self, comp: Competition, home: str, away: str) -> tuple[int, int] | None:
        """Score final d'une rencontre déjà jouée, ou None si introuvable.

        Sert à confronter un pronostic passé au résultat réel. On repart des
        matchs récents de l'équipe à domicile, que plusieurs sources publient
        déjà pour le calcul de forme : aucun endpoint supplémentaire, aucun
        crédit consommé.

        Un score introuvable n'est pas une anomalie — le match peut ne pas
        avoir encore été joué, ou être sorti de la fenêtre de forme. On
        renvoie None et le pronostic reste « en attente ».
        """
        for provider in self.providers:
            if not hasattr(provider, "form"):
                continue
            try:
                forme = provider.form(comp, home)
            except Exception:
                continue
            for rencontre in getattr(forme, "matches", None) or []:
                # Il faut le match joué À DOMICILE par cette équipe : c'est le
                # sens dans lequel le pronostic a été enregistré.
                if not rencontre.home:
                    continue
                if name_similarity(rencontre.opponent, away) < 0.80:
                    continue
                return int(round(rencontre.scored)), int(round(rencontre.conceded))
        return None

    def goal_scorers(self, comp: Competition, home: str, away: str) -> ScorerBoard | None:
        """Buteurs probables du match, ou None si le marché n'est pas publié.

        Rien n'est déduit ni estimé : sans cotes buteur, on ne renvoie rien
        plutôt qu'un classement fabriqué à partir de la forme collective.
        """
        if comp.sport != "football":
            return None
        for provider in self.providers:
            if isinstance(provider, TheOddsApiProvider):
                try:
                    return provider.goal_scorers(comp, home, away)
                except Exception:
                    log.info("buteurs indisponibles pour %s - %s", home, away)
                    return None
        return None

    def fixtures(self, comp: Competition) -> list[Fixture]:
        """Prochaines rencontres, fusionnées entre toutes les sources qui en ont.

        L'effectif d'une compétition est fusionné entre plusieurs sources et
        plusieurs saisons : il contient donc parfois des équipes qui n'y jouent
        plus, ou des adversaires de coupe venus d'une autre division. Un
        calendrier, lui, ne décrit que des matchs qui auront vraiment lieu.

        Ordre des sources, et pourquoi :

        1. **The Odds API** en premier. Son calendrier est le plus complet, et
           surtout c'est le seul périmètre où des cotes existent réellement.
        2. **API NHL** et **TheSportsDB** ensuite. Gratuites et sans clé, elles
           font vivre la fonction même quand aucune clé de cotes n'est posée —
           l'analyse repose alors sur les seules statistiques.

        Les doublons sont écartés : un même match publié par deux sources
        n'apparaît qu'une fois, sous le libellé de la source la plus fiable.
        """
        fusion: list[Fixture] = []

        def deja_vu(candidat: Fixture) -> bool:
            """Le même match, sous un autre nom ?

            Les sources ne nomment pas les clubs pareil : « Alavés » chez l'une,
            « Deportivo Alavés » chez l'autre ; « Stuttgart » contre « VfB
            Stuttgart ». Une comparaison exacte laisserait donc passer des
            doublons, et l'utilisateur verrait deux fois la même affiche.
            La date sert de garde-fou : deux clubs aux noms proches qui jouent
            un autre jour restent bien deux rencontres distinctes.
            """
            for garde in fusion:
                if (
                    candidat.starts_at and garde.starts_at
                    and candidat.starts_at.date() != garde.starts_at.date()
                ):
                    continue
                if (
                    name_similarity(candidat.home, garde.home) >= 0.72
                    and name_similarity(candidat.away, garde.away) >= 0.72
                ):
                    return True
            return False

        for provider in self.providers:
            if not hasattr(provider, "fixtures"):
                continue
            try:
                trouvees = provider.fixtures(comp) or []
            except Exception:  # une source défaillante n'en bloque pas d'autres
                log.info("calendrier indisponible via %s pour %s", provider.name, comp.label)
                continue
            for match in trouvees:
                if not deja_vu(match):
                    fusion.append(match)
        return sorted(
            fusion,
            key=lambda f: (f.starts_at is None, f.starts_at or datetime.max.replace(tzinfo=UTC)),
        )

    # -- collecte pour un match -------------------------------------------
    def collect(
        self,
        comp: Competition,
        home: str,
        away: str,
        with_news: bool = True,
        with_weather: bool = True,
    ) -> Bundle:
        """Dossier complet du match (recherche approfondie, en arrière-plan)."""
        bundle, _report = self.research.investigate(
            comp, home, away, with_news=with_news, with_weather=with_weather
        )
        return bundle

    def investigate(
        self,
        comp: Competition,
        home: str,
        away: str,
        with_news: bool = True,
        with_weather: bool = True,
    ):
        """Comme `collect`, mais renvoie aussi le rapport de recherche."""
        return self.research.investigate(
            comp, home, away, with_news=with_news, with_weather=with_weather
        )

    def market_reference(
        self, comp: Competition, home: str, away: str
    ) -> tuple[dict[str, float], Provenance, str] | None:
        """Probabilités 1X2 déduites des cotes de clôture de la saison.

        Combine le profil « à domicile » de l'équipe qui reçoit et le profil
        « à l'extérieur » de celle qui se déplace, par moyenne géométrique —
        une combinaison symétrique, sans coefficient arbitraire.

        Ce n'est PAS la cote du match : c'est ce que le marché accordait à
        ces deux équipes tout au long de la saison.
        """
        for provider in self.sources_for(comp):
            getter = getattr(provider, "market_ratings", None)
            if getter is None:
                continue
            try:
                got = getter(comp)
            except Exception:
                got = None
            if not got:
                continue
            profiles, prov = got
            key_home = best_match(home, profiles.keys(), threshold=0.75)
            key_away = best_match(away, profiles.keys(), threshold=0.75)
            if not key_home or not key_away:
                continue
            side_home = profiles[key_home].get("home")
            side_away = profiles[key_away].get("away")
            if not side_home or not side_away:
                continue

            # Moyenne géométrique des deux points de vue, puis normalisation.
            combined = [
                math.sqrt(max(side_home[i], 1e-6) * max(side_away[i], 1e-6))
                for i in range(3)
            ]
            total = sum(combined)
            if total <= 0:
                continue
            probs = {
                "home": combined[0] / total,
                "draw": combined[1] / total,
                "away": combined[2] / total,
            }
            detail = f"{prov.detail} ({key_home} / {key_away})"
            return probs, prov, detail
        return None

    def measured_league_average(self, comp: Competition) -> tuple[float, int, Provenance] | None:
        """Moyenne de buts mesurée sur la compétition, si une source la donne."""
        for provider in self.sources_for(comp):
            getter = getattr(provider, "league_average", None)
            if getter is None:
                continue
            try:
                got = getter(comp)
            except Exception:
                got = None
            if got:
                return got
        return None

    def league_context(self, comp: Competition, bundle: Bundle) -> dict[str, Any]:
        """Moyenne de référence de la compétition, calculée sur du réel si possible."""
        defaults = {
            "football": cfg.ENGINE.league_avg_goals_football,
            "hockey": cfg.ENGINE.league_avg_goals_hockey,
            "basket": cfg.ENGINE.league_avg_points_basket,
            "tennis": 0.0,
        }
        # 1) La saison complète mesurée sur les calendriers officiels : c'est
        #    la référence la plus large et la plus directe.
        measured = self.measured_league_average(comp)
        if measured:
            average, played, prov = measured
            bundle.track(prov)
            return {
                "avg_per_team": average,
                "estimated": True,
                "source": "saison complète",
                "n": played,
            }
        # 2) Le classement donne la même grandeur, sur la saison en cours.
        if bundle.standings:
            played = sum(s.played for s in bundle.standings.values())
            goals = sum(s.goals_for for s in bundle.standings.values())
            if played >= 20:
                return {
                    "avg_per_team": goals / played,
                    "estimated": True,
                    "source": "classement",
                    "n": played,
                }
        # 3) Sinon, la moyenne des matchs récupérés.
        samples = [
            (m.scored + m.conceded) / 2.0
            for form in (bundle.form_home, bundle.form_away)
            if form
            for m in form.matches
        ]
        if len(samples) >= 2 * cfg.ENGINE.min_matches:
            return {
                "avg_per_team": sum(samples) / len(samples),
                "estimated": True,
                "source": "forme récente",
                "n": len(samples),
            }
        return {
            "avg_per_team": defaults.get(comp.sport, 0.0),
            "estimated": False,
            "source": "valeur de référence",
            "n": len(samples),
        }


# ==========================================================================
# Historique local des prédictions
# ==========================================================================
class PredictionHistory:
    def __init__(self, path=None, limit: int = 500):
        self.path = str(path or cfg.HISTORY_FILE)
        self.limit = limit

    def _load(self) -> list[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def add(self, record: dict) -> None:
        rows = self._load()
        rows.append({"ts": datetime.now(UTC).isoformat(), **record})
        rows = rows[-self.limit :]
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def all(self) -> list[dict]:
        return self._load()

