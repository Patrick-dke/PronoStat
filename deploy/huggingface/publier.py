"""Assemble et publie l'API sur un Space Hugging Face.

Pourquoi un script plutôt qu'un simple `git push` : un Space se configure
par une en-tête YAML dans son `README.md` et par un `Dockerfile` à sa
racine. Or la racine du dépôt porte déjà un README de projet et un
Dockerfile qui lance Streamlit. Pousser le dépôt tel quel écraserait la
configuration du Space ; y ajouter l'en-tête polluerait le dépôt principal.

Ce script construit donc une copie de travail dans un dossier temporaire :
le code, plus le `Dockerfile` et le `README.md` propres au Space. Le dépôt
principal n'est jamais modifié.

    python deploy/huggingface/publier.py <utilisateur>/<nom-du-space>

Relançable autant de fois que nécessaire : chaque exécution publie l'état
courant du dossier.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RACINE = Path(__file__).resolve().parents[2]
ICI = Path(__file__).resolve().parent

# Ce que le Space a besoin d'exécuter. Volontairement explicite : une copie
# aveugle emporterait `.venv`, les caches et l'historique git, pour des
# centaines de mégaoctets inutiles.
FICHIERS = [
    "api.py", "engine.py", "config.py", "data_sources.py", "research.py",
    "requirements.txt", "requirements-api.txt",
]
# `web` est inclus : le Space sert alors aussi l'interface, ce qui donne
# une seconde adresse utilisable si Firebase venait a manquer.
DOSSIERS = ["agent", "web"]


def _jeton_hf() -> str:
    """Jeton d'écriture Hugging Face, s'il est disponible.

    Cherché dans l'environnement, puis dans `.env` — que git ignore, donc
    sans risque de publication accidentelle. Sa valeur n'est jamais affichée.
    """
    jeton = os.getenv("HF_TOKEN", "").strip()
    if jeton:
        return jeton
    try:
        from dotenv import dotenv_values

        return (dotenv_values(RACINE / ".env").get("HF_TOKEN") or "").strip()
    except Exception:
        return ""


def executer(cmd: list[str], cwd: Path) -> None:
    resultat = subprocess.run(cmd, cwd=cwd, text=True)
    if resultat.returncode != 0:
        raise SystemExit(f"Échec : {' '.join(cmd)}")


def main() -> None:
    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        raise SystemExit(
            "Usage : python deploy/huggingface/publier.py <utilisateur>/<space>\n"
            "Exemple : python deploy/huggingface/publier.py Patrick-dke/pronostat-api"
        )
    space = sys.argv[1]
    url = f"https://huggingface.co/spaces/{space}"

    manquants = [f for f in FICHIERS if not (RACINE / f).exists()]
    if manquants:
        raise SystemExit(f"Fichiers introuvables : {', '.join(manquants)}")

    with tempfile.TemporaryDirectory() as tmp:
        travail = Path(tmp) / "space"
        print(f"Préparation dans {travail}")
        travail.mkdir(parents=True)

        for nom in FICHIERS:
            shutil.copy2(RACINE / nom, travail / nom)
        for nom in DOSSIERS:
            shutil.copytree(
                RACINE / nom, travail / nom,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        # Le Dockerfile et le README du Space écrasent volontairement ceux du
        # projet : ce sont eux qui décrivent le service, pas l'interface.
        shutil.copy2(ICI / "Dockerfile", travail / "Dockerfile")
        shutil.copy2(ICI / "README.md", travail / "README.md")
        (travail / ".gitignore").write_text("__pycache__/\n*.pyc\n.cache/\n",
                                            encoding="utf-8")

        print(f"{len(FICHIERS)} fichiers et {len(DOSSIERS)} dossier(s) copiés.")

        executer(["git", "init", "-q", "-b", "main"], travail)
        executer(["git", "add", "-A"], travail)
        executer(["git", "commit", "-q", "-m", "Publier l'API PronoStat"], travail)
        # Deux façons de s'authentifier, dans cet ordre :
        #
        # 1. `HF_TOKEN`, lu dans l'environnement ou dans `.env` — pratique
        #    quand `hf auth login` n'aboutit pas. Le jeton n'est écrit que
        #    dans le dépôt temporaire, effacé à la sortie de ce bloc.
        # 2. À défaut, git demande identifiant et mot de passe.
        jeton = _jeton_hf()
        pousser_vers = (
            url.replace("https://", f"https://user:{jeton}@") if jeton else url
        )
        executer(["git", "remote", "add", "origin", pousser_vers], travail)

        print(f"\nPublication vers {url}")
        if jeton:
            print("Authentification par HF_TOKEN.\n")
        else:
            print("Identifiants demandés : votre nom d'utilisateur Hugging Face,")
            print("et un jeton d'accès en écriture comme mot de passe")
            print("(huggingface.co/settings/tokens, portée « write »).\n")
        # `--force` est ici le comportement voulu : le Space est un miroir de
        # ce dossier, pas un dépôt où l'on collabore.
        executer(["git", "push", "--force", "origin", "main"], travail)

    print(f"\nPublié. Construction en cours : {url}")
    print("Pensez à définir PRONOSTAT_API_TOKEN et ODDS_API_KEY dans")
    print("Settings puis Variables and secrets du Space.")


if __name__ == "__main__":
    main()
