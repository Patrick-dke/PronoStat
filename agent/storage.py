"""Stockage du journal des pronostics.

Deux implémentations interchangeables :

* `LocalFileStore` — un fichier JSON. Parfait en local, mais le disque des
  hébergeurs gratuits est **éphémère** : le journal disparaît à chaque
  redémarrage, et le taux de réussite ne peut jamais s'accumuler.
* `FirestoreStore` — une base externe, gratuite sur le plan Spark de
  Firebase. Le journal survit alors aux redémarrages.

Le choix est automatique : Firestore dès qu'un compte de service est
configuré, le fichier local sinon. Aucune configuration ne casse
l'application — au pire elle retombe sur le comportement précédent.

Le journal tient dans **un seul document**, sérialisé en JSON. C'est
volontaire : une lecture et une écriture par opération, donc un usage
dérisoire face aux 50 000 lectures et 20 000 écritures quotidiennes du
palier gratuit.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Protocol

log = logging.getLogger("pronostat.storage")

# Un document Firestore est limité à 1 Mio. On garde une marge confortable :
# 500 analyses suffisent très largement à établir un taux de réussite, et
# pèsent environ 150 Kio.
FIRESTORE_MAX_ENTRIES = 500

_SCOPES = ["https://www.googleapis.com/auth/datastore"]


class LedgerStore(Protocol):
    """Contrat minimal : charger et enregistrer une liste de lignes."""

    def load(self) -> list[dict]: ...
    def save(self, rows: list[dict]) -> None: ...
    @property
    def label(self) -> str: ...


class LocalFileStore:
    """Fichier JSON local. Écriture atomique via un fichier temporaire."""

    def __init__(self, path: str, limit: int = 2000):
        self.path = str(path)
        self.limit = limit

    @property
    def label(self) -> str:
        return "fichier local"

    def load(self) -> list[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def save(self, rows: list[dict]) -> None:
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rows[-self.limit:], fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


class FirestoreStore:
    """Journal conservé dans Firestore, via l'API REST.

    On passe par REST plutôt que par la bibliothèque cliente officielle :
    celle-ci entraîne gRPC et protobuf, une cinquantaine de mégaoctets qui
    ralentiraient chaque déploiement pour une seule lecture et une seule
    écriture par opération.
    """

    BASE = "https://firestore.googleapis.com/v1"

    def __init__(self, project_id: str, credentials, collection: str = "pronostat",
                 document: str = "ledger", limit: int = FIRESTORE_MAX_ENTRIES):
        self.project_id = project_id
        self._credentials = credentials
        self.collection = collection
        self.document = document
        self.limit = limit
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return "Firestore"

    @property
    def _url(self) -> str:
        return (
            f"{self.BASE}/projects/{self.project_id}/databases/(default)"
            f"/documents/{self.collection}/{self.document}"
        )

    def _headers(self) -> dict[str, str]:
        import google.auth.transport.requests as ga_requests

        if not self._credentials.valid:
            self._credentials.refresh(ga_requests.Request())
        return {"Authorization": f"Bearer {self._credentials.token}"}

    def load(self) -> list[dict]:
        import requests

        try:
            resp = requests.get(self._url, headers=self._headers(), timeout=15)
        except Exception as exc:
            log.warning("Firestore illisible (%s) — journal considéré vide", type(exc).__name__)
            return []
        if resp.status_code == 404:
            return []                    # document pas encore créé : normal
        if resp.status_code != 200:
            log.warning("Firestore a répondu %s à la lecture", resp.status_code)
            return []
        champ = ((resp.json() or {}).get("fields") or {}).get("entries") or {}
        brut = champ.get("stringValue") or "[]"
        try:
            data = json.loads(brut)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def save(self, rows: list[dict]) -> None:
        import requests

        charge = {
            "fields": {
                "entries": {
                    "stringValue": json.dumps(rows[-self.limit:], ensure_ascii=False)
                }
            }
        }
        with self._lock:
            try:
                resp = requests.patch(
                    self._url, headers=self._headers(), json=charge, timeout=20
                )
            except Exception as exc:
                log.warning("Firestore inaccessible (%s) — journal non enregistré",
                            type(exc).__name__)
                return
        if resp.status_code >= 300:
            log.warning("Firestore a refuse l'ecriture : %s", resp.status_code)


# Pourquoi le journal n'est pas dans Firestore, quand un compte de service a
# pourtant été configuré. Reste à None si rien n'a été configuré — cas normal,
# qui n'a pas à être signalé. Ne contient jamais de valeur secrète.
FALLBACK_REASON: str | None = None


def _service_account_info() -> dict | None:
    """Compte de service, s'il est configuré. Jamais journalisé."""
    import config as cfg

    global FALLBACK_REASON
    brut = cfg._secret("FIREBASE_SERVICE_ACCOUNT").strip()
    if not brut:
        return None
    # Les délimiteurs de bloc de code collés par mégarde sont la première
    # cause d'échec, devant tout le reste.
    if brut.startswith("```"):
        FALLBACK_REASON = (
            "La valeur commence par des accents graves (```) : ils ont été "
            "collés avec le contenu. Le secret doit commencer directement par "
            "une accolade { ."
        )
        return None
    try:
        info = json.loads(brut)
    except json.JSONDecodeError as exc:
        FALLBACK_REASON = (
            f"Le contenu n'est pas du JSON valide ({exc.msg}, ligne {exc.lineno}). "
            "Vérifiez que le fichier a été copié en entier, entre trois "
            "apostrophes ''' et non entre guillemets."
        )
        return None
    if not isinstance(info, dict):
        FALLBACK_REASON = "Le JSON lu n'est pas un objet."
        return None
    if not info.get("project_id"):
        FALLBACK_REASON = (
            "Le JSON ne contient pas de `project_id` : ce n'est probablement "
            "pas un fichier de compte de service."
        )
        return None
    return info


def make_store(path: str, limit: int = 2000) -> LedgerStore:
    """Firestore si un compte de service est configuré, fichier local sinon.

    Toute anomalie — dépendance absente, identifiants illisibles, projet
    introuvable — fait retomber sur le fichier local plutôt que d'empêcher
    l'application de démarrer. Un journal éphémère vaut mieux qu'une page
    en erreur.
    """
    global FALLBACK_REASON
    FALLBACK_REASON = None
    info = _service_account_info()
    if info is None:
        return LocalFileStore(path, limit)
    try:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=_SCOPES
        )
    except ImportError:
        FALLBACK_REASON = (
            "La bibliothèque `google-auth` n'est pas installée sur le serveur. "
            "Elle figure dans requirements.txt : un redéploiement devrait suffire."
        )
        return LocalFileStore(path, limit)
    except Exception as exc:
        FALLBACK_REASON = (
            f"Le compte de service est refusé ({type(exc).__name__}). "
            "La clé privée est souvent tronquée à la copie : elle doit "
            "contenir la ligne -----BEGIN PRIVATE KEY----- et sa fin."
        )
        return LocalFileStore(path, limit)
    log.info("Journal des pronostics : Firestore (projet %s)", info["project_id"])
    return FirestoreStore(info["project_id"], credentials)
