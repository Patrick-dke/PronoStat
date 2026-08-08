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
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, status
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
    research: dict[str, Any] | None = Field(
        default=None,
        description="Données pré-collectées. Archivées, pas encore exploitées "
                    "par le modèle (étape 5 du plan).",
    )


class ResultRequest(BaseModel):
    analysis_id: str
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


@app.get("/quota", dependencies=[Depends(require_token)])
def quota() -> dict[str, Any]:
    """Crédits restants. C'est cette route qui pilote la priorisation n8n."""
    hub = get_hub()
    return {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "providers": [
            {
                "provider": s.provider,
                "remaining": s.remaining,
                "period": s.period,
                "exhausted": s.exhausted,
            }
            for s in hub.quota_status()
        ],
        # Une analyse de football coûte trois marchés : h2h, totals, spreads.
        "cost_per_analysis": 3,
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
    charge = result.as_payload()
    charge.update(
        {
            "analysis_id": _analysis_id(req, comp),
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


def _analysis_id(req: AnalysisRequest, comp) -> str:
    """Même identifiant que le journal, pour que /result puisse le retrouver."""
    from agent.memory import _match_id

    return _match_id(req.sport, getattr(comp, "label", ""), req.home, req.away)
