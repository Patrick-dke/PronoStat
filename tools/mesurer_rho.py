"""Le paramètre Dixon-Coles gagne-t-il à être ajusté par championnat ?

Le modèle utilise une valeur unique et figée (`DIXON_COLES_RHO`, -0,08),
tirée de la littérature. La question naturelle est de l'ajuster à chaque
championnat par maximum de vraisemblance sur les scores réellement observés.

Ce script répond à cette question par la mesure, sur les fichiers de saison
openfootball déjà téléchargés — aucun crédit d'API n'est consommé.

    python tools/mesurer_rho.py

Lecture du résultat : le gain est une différence de log-vraisemblance. Un
test du rapport de vraisemblance sur **un** paramètre supplémentaire exige
environ **1,92** pour être significatif au seuil de 5 %. En deçà, l'ajustement
capte du bruit d'échantillonnage, pas une propriété du championnat.

Résultat au 9 août 2026 : gains de 0,0 à 1,5 selon les championnats, zéro en
Premier League. **L'ajustement n'a pas été retenu.** Relancez ce script quand
plusieurs saisons seront disponibles : la conclusion pourrait changer, la
méthode non.
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
from data_sources import DataHub  # noqa: E402
from engine import dixon_coles_matrix  # noqa: E402

# Seuil du test du rapport de vraisemblance, un degré de liberté, α = 5 %.
SEUIL_SIGNIFICATIF = 1.92


def scores_observes(provider, comp) -> list[tuple[int, int]]:
    """Scores finaux de la saison, tels que la source les publie."""
    got = provider._season_file(comp)
    if not got:
        return []
    sorties = []
    for match in got[0].get("matches") or []:
        score = provider._full_time_score(match)
        if score is not None:
            sorties.append((int(score[0]), int(score[1])))
    return sorties


def log_vraisemblance(scores, lam_home: float, lam_away: float, rho: float) -> float:
    matrice = dixon_coles_matrix(lam_home, lam_away, rho)
    borne = matrice.shape[0] - 1
    total = 0.0
    for buts_dom, buts_ext in scores:
        # Les scores fleuves dépassent la matrice : on les ramène à son bord
        # plutôt que de les écarter, pour ne pas biaiser la comparaison.
        total += math.log(max(matrice[min(buts_dom, borne), min(buts_ext, borne)], 1e-12))
    return total


def meilleur_rho(scores, lam_home: float, lam_away: float) -> tuple[float, float]:
    """Balayage fin plutôt qu'optimiseur : le domaine est borné et
    unidimensionnel, une recherche exhaustive est ici plus sûre qu'un
    algorithme susceptible de s'arrêter sur un optimum local."""
    meilleur, valeur = None, -math.inf
    for candidat in np.arange(-0.30, 0.1201, 0.005):
        v = log_vraisemblance(scores, lam_home, lam_away, float(candidat))
        if v > valeur:
            meilleur, valeur = float(candidat), v
    return meilleur, valeur


def main() -> None:
    logging.disable(logging.WARNING)
    hub = DataHub()
    provider = next(p for p in hub.providers if p.name == "openfootball")
    fige = cfg.ENGINE.dixon_coles_rho

    print(f"rho figé actuellement : {fige}")
    print(f"seuil de significativité : {SEUIL_SIGNIFICATIF} de log-vraisemblance\n")
    print(f"{'Championnat':18} {'matchs':>7} {'rho ajusté':>11} {'gain':>8}  verdict")

    retenus = 0
    for comp in cfg.competitions("football"):
        if comp.is_cup or not comp.openfootball_code:
            continue
        scores = scores_observes(provider, comp)
        if len(scores) < 100:
            print(f"{comp.label:18} {len(scores):7}   échantillon insuffisant")
            continue
        lam_home = sum(a for a, _ in scores) / len(scores)
        lam_away = sum(b for _, b in scores) / len(scores)
        base = log_vraisemblance(scores, lam_home, lam_away, fige)
        ajuste, valeur = meilleur_rho(scores, lam_home, lam_away)
        gain = valeur - base
        significatif = gain >= SEUIL_SIGNIFICATIF
        retenus += significatif
        print(
            f"{comp.label:18} {len(scores):7} {ajuste:11.3f} {gain:8.2f}  "
            + ("SIGNIFICATIF" if significatif else "bruit")
        )

    print()
    if retenus:
        print(f"{retenus} championnat(s) au-dessus du seuil : l'ajustement mérite")
        print("d'être reconsidéré, championnat par championnat.")
    else:
        print("Aucun championnat au-dessus du seuil. Garder la valeur figée :")
        print("ajuster ajouterait un paramètre sans gagner de pouvoir explicatif.")


if __name__ == "__main__":
    main()
