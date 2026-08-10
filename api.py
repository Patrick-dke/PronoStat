"""API HTTP de PronoStat — la couture entre n8n et le moteur.

Streamlit ne peut pas exposer d'endpoints : cette API est le service
distinct décrit à l'étape 1 de `N8N-ARCHITECTURE.md`. Elle n'implémente
**aucune logique d'analyse** ; elle appelle les mêmes modules que
l'interface (`agent/`, `engine.py`, `data_sources.py`), qui restent la seule
source de vérité.

Lancement local :

    uvicorn api:app --port 8000

Quatre routes :

    GET  /health              état du service, sans authentification
    GET  /quota               crédits restants, pour piloter l'orchestration
    POST /analysis            lance une analyse et renvoie le résultat
    GET  /analysis/{id}       relit une analyse archivée
    POST /result              enregistre le score réel après le match

⚠️ **Une analyse consomme des crédits d'API payants.** L'authentification
n'est donc pas optionnelle : sans jeton, n'importe qui pourrait épuiser le
quota mensuel en quelques minutes. Voir `require_token`.
"""

from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config as cfg
from agent import AnalysisAgent
from agent.memory import PredictionLedger
from data_sources import DataHub
from engine import __doc__ as _engine_doc  # noqa: F401  (présence vérifiée au démarrage)

UTC = timezone.utc
log = logging.getLogger("pronostat.api")

# Les versions du moteur viennent de `config` : l'interface et l'API
# produisent les mêmes analyses, les dupliquer ici les ferait diverger.
MODEL_VERSION = cfg.MODEL_VERSION
RESEARCH_VERSION = cfg.RESEARCH_VERSION
API_VERSION = "1.0"      # version du contrat HTTP, indépendante du moteur

app = FastAPI(
    title="PronoStat",
    version=API_VERSION,
    description="Couture HTTP entre n8n et le moteur d'analyse.",
)

# --------------------------------------------------------------------------
# Origines autorisées
# --------------------------------------------------------------------------
# Nécessaire dès que l'interface n'est plus servie par ce service — hébergée
# sur Firebase, par exemple. Une liste explicite, jamais `*` : cette API
# dépense un quota payant, et autoriser toutes les origines laisserait
# n'importe quel site tenter des appels depuis le navigateur de vos
# visiteurs. `ALLOWED_ORIGINS` permet d'en ajouter sans toucher au code.
_ORIGINES = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://pronostat-8a2a8.web.app,https://pronostat-8a2a8.firebaseapp.com",
    ).split(",")
    if o.strip()
]
if _ORIGINES:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ORIGINES,
        # Pas de cookies : l'authentification passe par un en-tête Bearer,
        # ce qui évite entièrement la classe des attaques CSRF.
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


# --------------------------------------------------------------------------
# Authentification
# --------------------------------------------------------------------------
def require_token(authorization: str = Header(default="")) -> None:
    """Refuse l'accès sans jeton valide.

    `PRONOSTAT_API_TOKEN` non défini ferme le service au lieu de l'ouvrir :
    une API qui dépense un quota payant ne doit jamais être accessible par
    défaut. Un oubli de configuration doit se voir immédiatement, pas se
    traduire par une facture ou un quota épuisé.

    La comparaison passe par `hmac.compare_digest` : une comparaison
    ordinaire s'interrompt au premier caractère différent et laisse deviner
    le jeton, caractère par caractère, en mesurant le temps de réponse.
    """
    attendu = os.getenv("PRONOSTAT_API_TOKEN", "").strip()
    if not attendu:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "PRONOSTAT_API_TOKEN n'est pas configuré : le service reste fermé.",
        )
    fourni = authorization.removeprefix("Bearer ").strip()
    if not fourni or not hmac.compare_digest(fourni, attendu):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Jeton absent ou invalide.")


# --------------------------------------------------------------------------
# Ressources partagées
# --------------------------------------------------------------------------
_hub: DataHub | None = None
_agent: AnalysisAgent | None = None


def get_agent() -> AnalysisAgent:
    """Agent unique, construit à la première demande.

    Le construire à l'import ralentirait le démarrage et ferait échouer le
    service entier si une source était momentanément injoignable.
    """
    global _hub, _agent
    if _agent is None:
        cfg.refresh_keys()
        _hub = DataHub()
        _agent = AnalysisAgent(_hub)
    return _agent


def get_hub() -> DataHub:
    get_agent()
    assert _hub is not None
    return _hub


# --------------------------------------------------------------------------
# Corps de requête
# --------------------------------------------------------------------------
class AnalysisRequest(BaseModel):
    """Demande d'analyse.

    L'étape 1 déclenche le pipeline existant, qui effectue lui-même sa
    collecte. Le champ `research` est accepté et **archivé tel quel**, sans
    être injecté dans le modèle : cette injection est l'étape 5 du plan.
    L'accepter en le laissant croire exploité serait trompeur.
    """

    sport: str = Field(..., examples=["football"])
    competition_key: str = Field(..., examples=["premier_league"])
    home: str = Field(..., examples=["Arsenal"])
    away: str = Field(..., examples=["Coventry City"])
    fixture_key: str | None = Field(
        default=None,
        description="Identifiant de rencontre cote orchestrateur.",
    )
    window: str | None = Field(
        default=None,
        description="Fenetre d'analyse : J-7, J-3, J-1, PRE_MATCH. Permet de "
                    "ne pas repayer des cotes pour une fenetre deja couverte.",
    )
    starts_at: str | None = Field(default=None, description="Coup d'envoi ISO 8601.")
    research: dict[str, Any] | None = Field(
        default=None,
        description="Données pré-collectées. Archivées, pas encore exploitées "
                    "par le modèle (étape 5 du plan).",
    )


class ResultRequest(BaseModel):
    analysis_id: str
    finished_at: str | None = None
    home_goals: int = Field(..., ge=0)
    away_goals: int = Field(..., ge=0)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, Any]:
    """Sans authentification : sert aux sondes de l'hébergeur."""
    return {
        "status": "ok",
        "api_version": API_VERSION,
        "model_version": MODEL_VERSION,
        "research_version": RESEARCH_VERSION,
        "token_configured": bool(os.getenv("PRONOSTAT_API_TOKEN", "").strip()),
    }


def _period_resets_at() -> str | None:
    """Date de remise à zéro du quota mensuel, si elle est connue.

    The Odds API réinitialise selon la date d'inscription du compte, pas le
    calendrier. Elle n'est donc pas déductible : on la renvoie seulement si
    `ODDS_QUOTA_RESET_DAY` la déclare. Deviner ferait dépenser le budget au
    mauvais rythme, ce qui est pire que l'absence — l'orchestrateur sait
    replier sur un mode qui ne demande pas cette date.
    """
    jour = os.getenv("ODDS_QUOTA_RESET_DAY", "").strip()
    if not jour.isdigit() or not 1 <= int(jour) <= 28:
        return None
    jour = int(jour)
    maintenant = datetime.now(UTC)
    mois, annee = maintenant.month, maintenant.year
    if maintenant.day >= jour:
        mois, annee = (1, annee + 1) if mois == 12 else (mois + 1, annee)
    return datetime(annee, mois, jour, tzinfo=UTC).isoformat(timespec="seconds")


@app.get("/quota", dependencies=[Depends(require_token)])
def quota() -> dict[str, Any]:
    """Crédits restants. C'est cette route qui pilote la priorisation n8n.

    Le compteur des cotes vient des en-têtes du fournisseur dès qu'un appel
    a eu lieu : c'est lui qui fait foi, pas une estimation locale.
    """
    hub = get_hub()
    etats = list(hub.quota_status())
    cotes = next((s for s in etats if s.provider == "the_odds_api"), None)
    limite = datetime.now(UTC) - timedelta(days=30)

    recentes = []
    for e in PredictionLedger().all():
        if (e.created_at or "") < limite.isoformat(timespec="seconds"):
            continue
        recentes.append({
            "analysis_id": e.id,
            "fixture_key": e.fixture_key or e.id,
            "window": e.window or None,
            "analysed_at": e.created_at,
        })

    return {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "odds_credits_remaining": cotes.remaining if cotes else None,
        "odds_credits_limit": cotes.limit if cotes else None,
        "odds_counter_authoritative": bool(cotes and cotes.authoritative),
        "period_resets_at": _period_resets_at(),
        "recent_analyses": recentes,
        # Une analyse coûte un crédit par marché ET par région réellement
        # interrogée. Trois marchés sur une seule région : 3 crédits.
        "cost_per_analysis": 3,
        "providers": [
            {
                "provider": s.provider,
                "remaining": s.remaining,
                "limit": s.limit,
                "period": s.period,
                "exhausted": s.exhausted,
            }
            for s in etats
        ],
    }


@app.get("/competitions", dependencies=[Depends(require_token)])
def competitions(sport: str | None = None) -> list[dict[str, Any]]:
    """Compétitions activées. Lecture locale : aucun appel réseau, aucun coût."""
    sports = [sport] if sport else list(cfg.SPORTS)
    return [
        {
            "sport": s,
            "key": c.key,
            "label": c.label,
            "is_cup": c.is_cup,
            "tier": c.tier,
        }
        for s in sports
        for c in cfg.competitions(s)
    ]


@app.get("/fixtures", dependencies=[Depends(require_token)])
def fixtures(sport: str, competition_key: str) -> list[dict[str, Any]]:
    """Rencontres programmées. Les endpoints calendrier sont gratuits."""
    comp = cfg.competition(sport, competition_key)
    if comp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compétition inconnue.")
    try:
        trouvees = get_hub().fixtures(comp)
    except Exception:
        return []      # un calendrier absent n'est pas une erreur de service
    return [
        {
            "home": f.home,
            "away": f.away,
            "starts_at": f.starts_at.isoformat(timespec="seconds") if f.starts_at else None,
            "label": f.label,
        }
        for f in trouvees
    ]


@app.get("/teams", dependencies=[Depends(require_token)])
def teams(sport: str, competition_key: str) -> dict[str, Any]:
    """Effectif d'une compétition, pour les menus de sélection."""
    comp = cfg.competition(sport, competition_key)
    if comp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compétition inconnue.")
    try:
        resultat = get_hub().roster(comp)
    except Exception:
        return {"teams": [], "coverage": None}
    return {"teams": resultat.names, "coverage": resultat.coverage}


@app.get("/history", dependencies=[Depends(require_token)])
def history() -> list[dict[str, Any]]:
    """Analyses passées, résolues ou non. Alimente la page « Mes analyses »."""
    return [
        {
            "analysis_id": e.id,
            "created_at": e.created_at,
            "sport": e.sport,
            "competition": e.competition,
            "home": e.home,
            "away": e.away,
            "recommendation": e.recommendation,
            "probability": e.probability,
            "confidence": e.confidence,
            "resolved": e.resolved,
            "hit": e.hit,
            "actual_home": e.actual_home,
            "actual_away": e.actual_away,
        }
        for e in reversed(PredictionLedger().all())
    ]


@app.get("/analysis/pending", dependencies=[Depends(require_token)])
def pending() -> list[dict[str, Any]]:
    """Analyses en attente de résultat. Alimente le workflow « Résultats ».

    Déclarée **avant** `/analysis/{analysis_id}` : sans cela, FastAPI ferait
    correspondre `pending` au paramètre de chemin et cette route ne serait
    jamais atteinte.
    """
    return [
        {
            "analysis_id": e.id,
            "fixture_key": e.fixture_key or e.id,
            "sport": e.sport,
            "competition": e.competition,
            "home": e.home,
            "away": e.away,
            "starts_at": e.starts_at,
            "created_at": e.created_at,
        }
        for e in PredictionLedger().pending()
    ]


@app.get("/score", dependencies=[Depends(require_token)])
def score(sport: str, competition_key: str, home: str, away: str) -> dict[str, Any]:
    """Score final d'une rencontre, si les sources l'ont publié.

    Tout statut autre que `finished` signifie « pas encore connu ». Traiter
    une absence comme un échec fausserait le Brier et la calibration : c'est
    la raison d'être du statut explicite.
    """
    comp = cfg.competition(sport, competition_key)
    if comp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compétition inconnue.")
    try:
        trouve = get_hub().final_score(comp, home, away)
    except Exception as exc:
        log.warning("score indisponible : %s", type(exc).__name__)
        return {"status": "source_unavailable", "home_goals": None, "away_goals": None}
    if trouve is None:
        return {"status": "not_published", "home_goals": None, "away_goals": None}
    return {
        "status": "finished",
        "home_goals": trouve[0],
        "away_goals": trouve[1],
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


@app.post("/analysis", dependencies=[Depends(require_token)])
def analyse(req: AnalysisRequest) -> dict[str, Any]:
    comp = cfg.competition(req.sport, req.competition_key)
    if comp is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Compétition inconnue : {req.sport}/{req.competition_key}",
        )
    try:
        result = get_agent().analyse_match(comp, req.home, req.away)
    except Exception as exc:
        log.exception("analyse impossible")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Analyse impossible : {type(exc).__name__}"
        ) from exc

    pred = result.prediction
    identifiant = _analysis_id(req, comp)
    # Metadonnees d'orchestration : elles viennent de n8n, pas du moteur.
    PredictionLedger().annotate(
        identifiant, fixture_key=req.fixture_key, window=req.window,
        starts_at=req.starts_at,
    )
    charge = result.as_payload()
    charge.update(
        {
            "analysis_id": identifiant,
            "model_version": MODEL_VERSION,
            "research_version": RESEARCH_VERSION,
            "data_timestamp": _oldest_source(pred),
            "prediction_timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "top_scores": [{"score": s, "probability": p} for s, p in pred.top_scores],
            # Score compatible avec le pronostic : c'est celui que l'interface
            # met en avant, l'API doit exposer la même chose.
            "pick_scores": [{"score": s, "probability": p} for s, p in pred.pick_scores],
            "markets": [
                {"key": l.key, "label": l.label, "probability": round(l.prob, 4)}
                for l in pred.lines
            ],
            "confidence": round(pred.confidence.score, 2),
            # Vide en fonctionnement normal. Non vide = n8n doit bloquer la
            # publication, conformément au nœud « Consistent ? ».
            "consistency": pred.consistency,
            "sources": [
                {
                    "source": p.source,
                    "detail": p.detail,
                    "fetched_at": p.fetched_at.isoformat(timespec="seconds"),
                    "from_cache": p.from_cache,
                }
                for p in pred.provenances
            ],
            "fixture_key": req.fixture_key,
            "window": req.window,
            "research_echo": req.research,
        }
    )
    return charge


@app.get("/analysis/{analysis_id}", dependencies=[Depends(require_token)])
def read_analysis(analysis_id: str) -> dict[str, Any]:
    """Relit une analyse archivée. Ne relance aucun calcul, ne coûte rien."""
    entree = next(
        (e for e in PredictionLedger().all() if e.id == analysis_id), None
    )
    if entree is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analyse inconnue.")
    return {
        "analysis_id": entree.id,
        "created_at": entree.created_at,
        "match": {
            "sport": entree.sport,
            "competition": entree.competition,
            "home": entree.home,
            "away": entree.away,
        },
        "model_version": entree.model_version,
        "research_version": entree.research_version,
        "data_timestamp": entree.data_timestamp,
        "recommendation": entree.recommendation,
        "market_key": entree.market_key,
        "probability": entree.probability,
        "confidence": entree.confidence,
        "outcome_probs": entree.outcome_probs,
        "resolved": entree.resolved,
        "actual": (
            None if not entree.resolved
            else {"home": entree.actual_home, "away": entree.actual_away,
                  "hit": entree.hit}
        ),
    }


@app.post("/result", dependencies=[Depends(require_token)])
def record_result(req: ResultRequest) -> dict[str, Any]:
    """Enregistre le score réel et tranche le pronostic.

    Un marché que le score final ne suffit pas à trancher — vainqueur après
    prolongations, puck line, sets — reste volontairement non résolu. Le
    signaler par `resolved: false` vaut mieux qu'un verdict inventé.
    """
    ledger = PredictionLedger()
    tranche = ledger.resolve(req.analysis_id, req.home_goals, req.away_goals)
    entree = next((e for e in ledger.all() if e.id == req.analysis_id), None)
    if entree is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analyse inconnue.")
    return {
        "analysis_id": req.analysis_id,
        "resolved": tranche,
        "hit": entree.hit,
        "reason": None if tranche else "Marché non évaluable à partir du seul score final.",
    }


def _oldest_source(prediction) -> str | None:
    """Fraicheur de l'analyse : la donnee la PLUS ANCIENNE, pas la plus
    recente. Retenir la meilleure flatterait le bilan."""
    from agent.memory import _oldest_data

    return _oldest_data(prediction)


# --------------------------------------------------------------------------
# Interface web
# --------------------------------------------------------------------------
# Servie par ce même service, à dessein. Une origine unique évite toute
# configuration CORS et n'impose qu'un seul hébergement. Le montage vient
# après les routes : monter « / » en premier les masquerait toutes.
_WEB = Path(__file__).resolve().parent / "web"
if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
else:  # pragma: no cover - déploiement sans interface
    log.warning("dossier web/ absent : l'API fonctionne, sans interface")


def _analysis_id(req: AnalysisRequest, comp) -> str:
    """Même identifiant que le journal, pour que /result puisse le retrouver."""
    from agent.memory import _match_id

    return _match_id(req.sport, getattr(comp, "label", ""), req.home, req.away)
