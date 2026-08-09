"""Module de mémoire : historique, calibration et propositions de réglage.

Responsabilité unique : garder trace de ce que l'agent a annoncé, confronter
ses annonces aux résultats réels quand ils deviennent connus, et en tirer des
statistiques de performance.

⚠️ Règle stricte : **l'agent ne modifie jamais ses réglages tout seul.**
Les ajustements sont enregistrés comme *propositions*, à accepter ou refuser
explicitement. Une proposition acceptée est écrite dans un fichier de
surcharge ; c'est l'utilisateur qui décide de la mettre en service.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import config as cfg
from . import storage

UTC = timezone.utc

LEDGER_FILE = cfg.CACHE_DIR / "ledger.json"
PROPOSALS_FILE = cfg.CACHE_DIR / "tuning_proposals.json"
OVERRIDES_FILE = cfg.CACHE_DIR / "parameter_overrides.json"


# ==========================================================================
# Journal des prédictions
# ==========================================================================
@dataclass
class LedgerEntry:
    """Une analyse produite, éventuellement confrontée à son résultat."""

    id: str
    created_at: str
    sport: str
    competition: str
    home: str
    away: str
    market_key: str
    recommendation: str
    probability: float
    confidence: float
    odds: float | None = None
    fingerprint: str = ""
    outcome_probs: dict[str, float] = field(default_factory=dict)
    # --- versions : sans elles, le backtesting compare des prédictions
    # issues de moteurs différents en croyant mesurer une seule méthode ---
    model_version: str = ""
    research_version: str = ""
    # Fraîcheur de la donnée la plus ancienne ayant servi à l'analyse. Une
    # prédiction juste fondée sur des données périmées ne vaut pas une
    # prédiction juste fondée sur des données fraîches : l'écart doit rester
    # mesurable après coup.
    data_timestamp: str | None = None
    # --- métadonnées d'orchestration, posées par l'API HTTP ---
    # `window` (J-7, J-3, J-1, PRE_MATCH) permet à l'orchestrateur de ne pas
    # repayer des cotes pour une fenêtre déjà couverte. Un simple plancher
    # horaire ré-analyserait un match vu à J-3 il y a 25 h, pour rien.
    fixture_key: str = ""
    window: str = ""
    starts_at: str | None = None
    # --- rempli plus tard, quand le résultat est connu ---
    resolved_at: str | None = None
    actual_home: int | None = None
    actual_away: int | None = None
    hit: bool | None = None

    @property
    def resolved(self) -> bool:
        return self.hit is not None


def _oldest_data(prediction) -> str | None:
    """Horodatage de la donnée la plus ancienne ayant servi à l'analyse.

    C'est la plus ancienne, non la plus récente, qui qualifie l'analyse : une
    prédiction n'est pas fraîche parce qu'une de ses sources l'était, elle
    l'est si toutes le sont. Retenir la meilleure flatterait le bilan.
    """
    dates = [
        p.fetched_at for p in getattr(prediction, "provenances", None) or []
        if getattr(p, "fetched_at", None) is not None
    ]
    return min(dates).isoformat(timespec="seconds") if dates else None


def _match_id(sport: str, competition: str, home: str, away: str) -> str:
    from data_sources import normalize_name

    return "|".join(
        [sport, competition, normalize_name(home), normalize_name(away)]
    )


class PredictionLedger:
    """Stockage local, simple et lisible, des analyses et de leurs résultats."""

    def __init__(self, path=None, limit: int = 2000, store=None):
        self.path = str(path or LEDGER_FILE)
        self.limit = limit
        self._lock = threading.Lock()
        # Un chemin explicite signale un usage local — tests, ou machine de
        # développement : on n'ira pas chercher une base externe. Sans chemin,
        # on prend le meilleur stockage disponible.
        if store is not None:
            self._store = store
        elif path is not None:
            self._store = storage.LocalFileStore(self.path, limit)
        else:
            self._store = storage.make_store(self.path, limit)

    @property
    def storage_label(self) -> str:
        """Où le journal est conservé, pour l'afficher à l'utilisateur."""
        return self._store.label

    # -- accès au stockage ----------------------------------------------
    def _load(self) -> list[dict]:
        return self._store.load()

    def _save(self, rows: list[dict]) -> None:
        self._store.save(rows)

    # -- écriture -------------------------------------------------------
    def record(self, decision, prediction, competition_label: str) -> LedgerEntry:
        entry = LedgerEntry(
            id=_match_id(prediction.sport, competition_label, prediction.home, prediction.away),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            sport=prediction.sport,
            competition=competition_label,
            home=prediction.home,
            away=prediction.away,
            market_key=getattr(prediction.main_pick, "key", "") or "",
            recommendation=decision.recommendation,
            probability=round(decision.probability, 4),
            confidence=round(decision.confidence, 2),
            odds=decision.odds,
            fingerprint=decision.fingerprint,
            outcome_probs={k: round(v, 4) for k, v in prediction.outcome_probs.items()},
            model_version=cfg.MODEL_VERSION,
            research_version=cfg.RESEARCH_VERSION,
            data_timestamp=_oldest_data(prediction),
        )
        with self._lock:
            rows = self._load()
            # Une nouvelle analyse du même match remplace la précédente.
            rows = [
                r for r in rows
                if not (r.get("id") == entry.id and r.get("hit") is None)
            ]
            rows.append(asdict(entry))
            self._save(rows)
        return entry

    def annotate(self, entry_id: str, **champs: Any) -> bool:
        """Complète une entrée avec des métadonnées d'orchestration.

        Séparé de `record()` à dessein : ces champs viennent de
        l'orchestrateur, pas du moteur. Les faire transiter par
        `analyse_match()` imposerait à l'interface Streamlit de connaître une
        notion de « fenêtre » qui ne la concerne pas.

        Seuls les champs existants sont acceptés — une clé inconnue est
        ignorée plutôt que d'écrire dans le journal une donnée qu'aucune
        version ne saura relire.
        """
        connus = {f.name for f in fields(LedgerEntry)}
        retenus = {k: v for k, v in champs.items() if k in connus and v is not None}
        if not retenus:
            return False
        with self._lock:
            rows = self._load()
            touche = False
            for row in rows:
                if row.get("id") == entry_id:
                    row.update(retenus)
                    touche = True
            if touche:
                self._save(rows)
            return touche

    def resolve(self, entry_id: str, home_goals: int, away_goals: int) -> bool:
        """Renseigne le résultat réel et détermine si le pronostic est passé."""
        with self._lock:
            rows = self._load()
            touched = False
            for row in rows:
                if row.get("id") != entry_id or row.get("hit") is not None:
                    continue
                row["actual_home"] = int(home_goals)
                row["actual_away"] = int(away_goals)
                row["resolved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
                row["hit"] = evaluate_market(
                    row.get("market_key", ""), int(home_goals), int(away_goals)
                )
                touched = touched or row["hit"] is not None
            if touched:
                self._save(rows)
            return touched

    # -- lecture --------------------------------------------------------
    def all(self) -> list[LedgerEntry]:
        """Journal complet, tolérant aux entrées d'autres versions.

        Deux directions à supporter, maintenant que le format porte des
        numéros de version :

        * une entrée **ancienne** n'a pas les champs récents — les valeurs
          par défaut du dataclass s'en chargent ;
        * une entrée **plus récente** en a de nouveaux, écrits par une version
          déployée ailleurs, sur le même Firestore. Les passer tels quels
          lèverait `TypeError` et rendrait tout l'historique illisible pour
          une seule clé inconnue. On les ignore.
        """
        connus = {f.name for f in fields(LedgerEntry)}
        entrees = []
        for row in self._load():
            if "id" not in row:
                continue
            try:
                entrees.append(LedgerEntry(**{k: v for k, v in row.items() if k in connus}))
            except (TypeError, ValueError):
                continue          # ligne corrompue : on saute, on ne casse pas
        return entrees

    def pending(self) -> list[LedgerEntry]:
        return [e for e in self.all() if not e.resolved]

    def resolved(self) -> list[LedgerEntry]:
        return [e for e in self.all() if e.resolved]


# ==========================================================================
# Évaluation d'un marché face au score réel
# ==========================================================================
def evaluate_market(market_key: str, home_goals: int, away_goals: int) -> bool | None:
    """Le pronostic est-il passé ? `None` si le marché n'est pas évaluable ici.

    Volontairement limité aux marchés dont le score final suffit à trancher.
    Un marché non évaluable reste non résolu plutôt que d'être compté au
    hasard — un taux de réussite faux serait pire que pas de taux du tout.
    """
    if not market_key:
        return None
    diff = home_goals - away_goals
    total = home_goals + away_goals

    if market_key == "1x2_home":
        return diff > 0
    if market_key == "1x2_away":
        return diff < 0
    if market_key == "1x2_draw":
        return diff == 0
    if market_key == "dc_1x":
        return diff >= 0
    if market_key == "dc_x2":
        return diff <= 0
    if market_key == "dc_12":
        return diff != 0
    if market_key == "btts_yes":
        return home_goals > 0 and away_goals > 0
    if market_key == "btts_no":
        return home_goals == 0 or away_goals == 0
    if market_key.startswith("total_over_"):
        return _threshold(market_key, "total_over_", lambda line: total > line)
    if market_key.startswith("total_under_"):
        return _threshold(market_key, "total_under_", lambda line: total < line)
    if market_key.startswith("hcp_home_-"):
        return _threshold(market_key, "hcp_home_-", lambda line: diff > line)
    if market_key.startswith("hcp_away_-"):
        return _threshold(market_key, "hcp_away_-", lambda line: -diff > line)
    # Vainqueur avec prolongations, puck line, sets : le score de temps
    # réglementaire ne suffit pas à trancher de façon sûre.
    return None


def _threshold(key: str, prefix: str, test) -> bool | None:
    try:
        return bool(test(float(key[len(prefix):])))
    except (TypeError, ValueError):
        return None


# ==========================================================================
# Statistiques de performance
# ==========================================================================
@dataclass
class CalibrationBin:
    low: float
    high: float
    count: int = 0
    predicted: float = 0.0
    observed: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.low:.0%}–{self.high:.0%}"

    @property
    def gap(self) -> float:
        return self.observed - self.predicted


@dataclass
class PerformanceReport:
    """Ce que valent réellement les annonces de l'agent, une fois vérifiées."""

    resolved: int = 0
    hits: int = 0
    brier: float | None = None
    bins: list[CalibrationBin] = field(default_factory=list)
    by_market: dict[str, tuple[int, int]] = field(default_factory=dict)
    pending: int = 0

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.resolved if self.resolved else None

    @property
    def average_predicted(self) -> float | None:
        if not self.bins or not self.resolved:
            return None
        total = sum(b.predicted * b.count for b in self.bins)
        return total / self.resolved

    @property
    def bias(self) -> float | None:
        """Positif = l'agent est trop prudent ; négatif = trop optimiste."""
        rate, predicted = self.hit_rate, self.average_predicted
        if rate is None or predicted is None:
            return None
        return rate - predicted

    @property
    def is_meaningful(self) -> bool:
        """En dessous de 20 résultats, aucune conclusion n'est solide."""
        return self.resolved >= 20


class PerformanceAnalyst:
    """Calcule calibration et taux de réussite à partir du journal."""

    BINS = ((0.0, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01))

    def report(self, entries: Iterable[LedgerEntry]) -> PerformanceReport:
        entries = list(entries)
        resolved = [e for e in entries if e.resolved]
        report = PerformanceReport(
            resolved=len(resolved),
            hits=sum(1 for e in resolved if e.hit),
            pending=len(entries) - len(resolved),
        )
        if not resolved:
            return report

        report.brier = round(
            sum((e.probability - (1.0 if e.hit else 0.0)) ** 2 for e in resolved)
            / len(resolved),
            4,
        )

        for low, high in self.BINS:
            bucket = [e for e in resolved if low <= e.probability < high]
            if not bucket:
                continue
            report.bins.append(
                CalibrationBin(
                    low=low,
                    high=min(high, 1.0),
                    count=len(bucket),
                    predicted=sum(e.probability for e in bucket) / len(bucket),
                    observed=sum(1 for e in bucket if e.hit) / len(bucket),
                )
            )

        for entry in resolved:
            family = entry.market_key.split("_")[0] or "autre"
            count, hits = report.by_market.get(family, (0, 0))
            report.by_market[family] = (count + 1, hits + (1 if entry.hit else 0))
        return report


# ==========================================================================
# Propositions de réglage — jamais appliquées sans validation
# ==========================================================================
@dataclass
class TuningProposal:
    id: str
    created_at: str
    parameter: str
    current: float
    proposed: float
    rationale: str
    evidence: str
    status: str = "pending"      # pending | accepted | rejected
    decided_at: str | None = None


class TuningAdvisor:
    """Suggère des ajustements de paramètres, et les soumet à validation.

    Aucune suggestion n'est appliquée automatiquement : elle est écrite dans
    un fichier de propositions. L'acceptation écrit la valeur dans un fichier
    de surcharge que l'utilisateur choisit — ou non — de mettre en service.
    """

    def __init__(self, path=None, overrides_path=None):
        self.path = str(path or PROPOSALS_FILE)
        self.overrides_path = str(overrides_path or OVERRIDES_FILE)
        self._lock = threading.Lock()

    # -- disque ---------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, rows: list[dict]) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- analyse --------------------------------------------------------
    def suggest(self, report: PerformanceReport) -> list[TuningProposal]:
        """Propose des ajustements — seulement si les données le justifient."""
        if not report.is_meaningful:
            return []

        proposals: list[TuningProposal] = []
        now = datetime.now(UTC).isoformat(timespec="seconds")
        bias = report.bias

        if bias is not None and bias <= -0.07:
            # L'agent annonce plus qu'il ne réalise : donner plus de poids au
            # marché, qui est mieux calibré que nos statistiques.
            current = cfg.ENGINE.market_weight
            proposals.append(
                TuningProposal(
                    id=f"market_weight_{now}",
                    created_at=now,
                    parameter="MARKET_WEIGHT",
                    current=current,
                    proposed=round(min(0.85, current + 0.05), 2),
                    rationale="Les probabilités annoncées sont trop optimistes",
                    evidence=(
                        f"{report.hits}/{report.resolved} pronostics réussis pour "
                        f"{report.average_predicted:.0%} annoncés en moyenne"
                    ),
                )
            )
        elif bias is not None and bias >= 0.07:
            current = cfg.ENGINE.market_weight
            proposals.append(
                TuningProposal(
                    id=f"market_weight_{now}",
                    created_at=now,
                    parameter="MARKET_WEIGHT",
                    current=current,
                    proposed=round(max(0.35, current - 0.05), 2),
                    rationale="Les probabilités annoncées sont trop prudentes",
                    evidence=(
                        f"{report.hits}/{report.resolved} pronostics réussis pour "
                        f"{report.average_predicted:.0%} annoncés en moyenne"
                    ),
                )
            )

        # Un seuil de valeur trop permissif se voit sur les paris signalés.
        weak = [b for b in report.bins if b.count >= 8 and b.gap <= -0.12]
        if weak:
            current = cfg.ENGINE.value_threshold
            proposals.append(
                TuningProposal(
                    id=f"value_threshold_{now}",
                    created_at=now,
                    parameter="VALUE_THRESHOLD",
                    current=current,
                    proposed=round(min(0.15, current + 0.02), 3),
                    rationale="Trop d'opportunités signalées ne se confirment pas",
                    evidence=f"Écart de {weak[0].gap:+.0%} sur la tranche {weak[0].label}",
                )
            )
        return proposals

    # -- cycle de vie ---------------------------------------------------
    def register(self, proposals: list[TuningProposal]) -> list[TuningProposal]:
        """Enregistre les nouvelles propositions, sans doublon de paramètre."""
        with self._lock:
            rows = self._load()
            pending = {r["parameter"] for r in rows if r.get("status") == "pending"}
            added = []
            for proposal in proposals:
                if proposal.parameter in pending:
                    continue
                rows.append(asdict(proposal))
                added.append(proposal)
            if added:
                self._save(rows)
            return added

    def pending(self) -> list[TuningProposal]:
        return [
            TuningProposal(**row) for row in self._load()
            if row.get("status") == "pending"
        ]

    def decide(self, proposal_id: str, accept: bool) -> bool:
        """Accepte ou refuse une proposition. L'acceptation N'APPLIQUE RIEN.

        Elle écrit la valeur dans `parameter_overrides.json`, que
        l'utilisateur peut reporter dans son fichier de configuration s'il le
        souhaite. Le moteur en cours d'exécution n'est jamais modifié.
        """
        with self._lock:
            rows = self._load()
            target = None
            for row in rows:
                if row.get("id") == proposal_id and row.get("status") == "pending":
                    row["status"] = "accepted" if accept else "rejected"
                    row["decided_at"] = datetime.now(UTC).isoformat(timespec="seconds")
                    target = row
                    break
            if target is None:
                return False
            self._save(rows)

        if accept:
            self._write_override(target["parameter"], target["proposed"])
        return True

    def _write_override(self, parameter: str, value: Any) -> None:
        try:
            with open(self.overrides_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data[parameter] = value
        data["_note"] = (
            "Valeurs acceptées, à reporter manuellement dans .env pour "
            "entrer en vigueur. Rien n'est appliqué automatiquement."
        )
        tmp = f"{self.overrides_path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.overrides_path)
        except OSError:
            pass

    def accepted_overrides(self) -> dict[str, Any]:
        try:
            with open(self.overrides_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {k: v for k, v in data.items() if not k.startswith("_")} \
                if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


# ==========================================================================
# Confrontation automatique aux résultats réels
# ==========================================================================
# Délai minimal entre l'analyse et la recherche du score. Un match dure deux
# heures, et les sources gratuites mettent un moment à publier : chercher
# trop tôt ne trouve rien et gaspille des appels.
DELAI_AVANT_RESOLUTION_H = 6.0


def resolve_pending(ledger: PredictionLedger, hub, max_lookups: int = 12) -> int:
    """Confronte les pronostics en attente aux scores réellement obtenus.

    Sans cette étape, le journal accumulait des analyses éternellement « en
    attente » : rien n'appelait jamais `resolve()`, et le taux de réussite
    ne pouvait donc jamais se former. Un score affiché sans être vérifié ne
    vaut rien — c'est précisément ce qu'on reproche aux annonces de
    performance invérifiables.

    Trois précautions :

    * on ignore les rencontres trop récentes : un score n'est publié qu'après
      le coup de sifflet final, et les sources mettent un moment à suivre ;
    * on borne le nombre de recherches par exécution, pour ne pas transformer
      l'ouverture de la page en longue attente ;
    * un score introuvable laisse simplement le pronostic en attente, sans
      rien inventer ni marquer d'échec.

    Renvoie le nombre de pronostics résolus.
    """
    attente = ledger.pending()
    if not attente:
        return 0

    limite = datetime.now(UTC) - timedelta(hours=DELAI_AVANT_RESOLUTION_H)
    resolus = 0
    examines = 0
    for entree in attente:
        if examines >= max_lookups:
            break
        try:
            cree = datetime.fromisoformat(entree.created_at)
        except (TypeError, ValueError):
            continue
        if cree.tzinfo is None:
            cree = cree.replace(tzinfo=UTC)
        if cree > limite:
            continue          # match probablement pas encore joué

        comp = next(
            (c for c in cfg.competitions(entree.sport) if c.label == entree.competition),
            None,
        )
        if comp is None:
            continue

        examines += 1
        try:
            score = hub.final_score(comp, entree.home, entree.away)
        except Exception:
            continue
        if score is None:
            continue
        if ledger.resolve(entree.id, score[0], score[1]):
            resolus += 1
    return resolus
