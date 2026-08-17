"""Prépare les données que l'interface Firebase servira.

Pourquoi ce script existe : Firebase Hosting ne sert que des fichiers, il ne
fait pas tourner de Python. Et l'hébergement d'un conteneur gratuit s'est
révélé introuvable — Hugging Face réserve désormais les Spaces Docker aux
comptes payants.

Plutôt que d'exposer les clés d'API dans le navigateur, ce qui permettrait à
n'importe quel visiteur d'épuiser le quota, le moteur tourne ici et publie
son résultat. L'interface en ligne lit un fichier statique : elle affiche de
vraies analyses, sans jamais approcher une clé.

    python tools/publier_donnees.py              # calendriers + historique
    python tools/publier_donnees.py --analyser   # + analyse les affiches
    python tools/publier_donnees.py --analyser --max 3

Les calendriers sont gratuits ; chaque analyse coûte trois crédits de cotes.
`--max` borne la dépense, ce qui rend l'exécution automatique tenable.

Puis `firebase deploy --only hosting`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
from agent import AnalysisAgent  # noqa: E402
from agent.memory import PredictionLedger  # noqa: E402
from data_sources import DataHub  # noqa: E402

UTC = timezone.utc
SORTIE = Path(__file__).resolve().parents[1] / "web" / "data.json"

# Nombre d'affiches analysées par compétition quand `--analyser` est passé.
# Chaque analyse consomme des crédits de cotes : le plafond est volontaire.
ANALYSES_PAR_COMPETITION = 2


def collecter(hub: DataHub) -> dict:
    """Calendriers de toutes les compétitions activées. Appels gratuits."""
    competitions, affiches = [], []
    for sport in cfg.SPORTS:
        for comp in cfg.competitions(sport):
            competitions.append({
                "sport": sport, "key": comp.key,
                "label": comp.label, "tier": comp.tier, "is_cup": comp.is_cup,
            })
            try:
                trouvees = hub.fixtures(comp)
            except Exception:
                continue
            for f in trouvees:
                affiches.append({
                    "sport": sport,
                    "competition": comp.label,
                    "competition_key": comp.key,
                    "home": f.home,
                    "away": f.away,
                    "starts_at": f.starts_at.isoformat(timespec="seconds")
                                 if f.starts_at else None,
                })
            print(f"  {comp.label:34} {len(trouvees):3} affiches")
    return {"competitions": competitions, "fixtures": affiches}


def _plafond() -> int:
    """Nombre maximal d'analyses, depuis `--max`.

    Un `--max` présent mais illisible vaut zéro, pas « sans limite » : la
    valeur vient d'une variable d'environnement calculée par le workflow, et
    se tromper dans ce sens dépenserait tout le quota de cotes d'un coup.
    """
    if "--max" in sys.argv:
        i = sys.argv.index("--max")
        brut = sys.argv[i + 1].strip() if i + 1 < len(sys.argv) else ""
        if not brut.isdigit():
            print(f"  --max illisible ({brut!r}) : aucune analyse lancée")
            return 0
        return int(brut)
    return 10**6


def analyser(hub: DataHub, affiches: list[dict]) -> None:
    """Analyse les prochaines affiches. Les résultats rejoignent le journal.

    Consomme du quota : on se limite aux compétitions majeures et aux
    rencontres les plus proches, celles qui intéressent réellement.
    """
    agent = AnalysisAgent(hub)
    plafond, faites = _plafond(), 0
    par_competition: dict[str, int] = {}
    for a in sorted(affiches, key=lambda x: x["starts_at"] or "9999"):
        if faites >= plafond:
            print(f"  plafond de {plafond} analyses atteint")
            break
        cle = a["competition_key"]
        if par_competition.get(cle, 0) >= ANALYSES_PAR_COMPETITION:
            continue
        comp = cfg.competition(a["sport"], cle)
        if comp is None or comp.tier != 1:
            continue
        par_competition[cle] = par_competition.get(cle, 0) + 1
        try:
            r = agent.analyse_match(comp, a["home"], a["away"])
            faites += 1
            print(f"  {a['home'][:20]:20} - {a['away'][:20]:20} "
                  f"{r.decision.recommendation[:28]:28} conf {r.decision.confidence:.1f}")
        except Exception as exc:
            print(f"  {a['home'][:20]:20} - {a['away'][:20]:20} echec : {type(exc).__name__}")


def historique() -> list[dict]:
    """Analyses archivées, la plus récente d'abord.

    Lève si le journal n'a pas pu être lu. Une lecture ratée renvoie une liste
    vide, indiscernable d'un journal réellement vide — et publier cette
    liste-là remplacerait toutes les analyses de l'application par rien.
    """
    journal = PredictionLedger()
    entrees = journal.all()
    if not journal.lecture_fiable:
        raise RuntimeError(
            "Journal des analyses illisible : publication annulée. "
            "Le fichier data.json existant reste en place."
        )
    return [
        {
            "analysis_id": e.id, "created_at": e.created_at,
            "sport": e.sport, "competition": e.competition,
            "home": e.home, "away": e.away,
            "recommendation": e.recommendation,
            "probability": e.probability, "confidence": e.confidence,
            "outcome_probs": e.outcome_probs,
            "resolved": e.resolved, "hit": e.hit,
            "actual_home": e.actual_home, "actual_away": e.actual_away,
        }
        for e in reversed(entrees)
    ]


def main() -> None:
    # On tait le bavardage des sources — une compétition sans calendrier au
    # mois d'août n'est pas une anomalie — mais surtout pas les avertissements
    # du stockage : ce sont eux qui disent pourquoi un journal est illisible.
    # `logging.disable()` ne le permettrait pas, il coupe tout sans distinction.
    logging.basicConfig(level=logging.WARNING, format="  %(message)s")
    logging.getLogger("pronostat").setLevel(logging.ERROR)
    logging.getLogger("pronostat.storage").setLevel(logging.WARNING)

    hub = DataHub()

    print("Calendriers")
    donnees = collecter(hub)

    if "--analyser" in sys.argv:
        print("\nAnalyses")
        analyser(hub, donnees["fixtures"])

    try:
        donnees["analyses"] = historique()
    except RuntimeError as exc:
        # Sortie en erreur, volontairement : en intégration continue, l'étape
        # de déploiement qui suit ne doit pas s'exécuter avec un fichier
        # amputé de tout son historique.
        raise SystemExit(f"\n{exc}") from exc

    donnees["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    donnees["model_version"] = cfg.MODEL_VERSION

    SORTIE.write_text(
        json.dumps(donnees, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    taille = SORTIE.stat().st_size / 1024
    print(f"\n{SORTIE.name} : {len(donnees['fixtures'])} affiches, "
          f"{len(donnees['analyses'])} analyses, {taille:.0f} Kio")
    print("Publier avec : firebase deploy --only hosting")


if __name__ == "__main__":
    main()
