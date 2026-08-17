"""Stockage du journal : fichier local et Firestore, et bascule entre les deux."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
from agent import storage  # noqa: E402
from agent.memory import PredictionLedger  # noqa: E402


class TestLocalFileStore:
    def test_round_trip(self, tmp_path):
        store = storage.LocalFileStore(str(tmp_path / "l.json"))
        assert store.load() == []
        store.save([{"id": "a"}])
        assert store.load() == [{"id": "a"}]

    def test_a_corrupted_file_reads_as_empty(self, tmp_path):
        """Un journal illisible ne doit jamais empêcher l'application de démarrer."""
        chemin = tmp_path / "l.json"
        chemin.write_text("ceci n'est pas du json", encoding="utf-8")
        assert storage.LocalFileStore(str(chemin)).load() == []

    def test_the_limit_keeps_the_most_recent(self, tmp_path):
        store = storage.LocalFileStore(str(tmp_path / "l.json"), limit=3)
        store.save([{"n": i} for i in range(10)])
        assert [r["n"] for r in store.load()] == [7, 8, 9]


class TestStoreSelection:
    def test_no_service_account_means_local(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT", raising=False)
        assert isinstance(storage.make_store(str(tmp_path / "l.json")),
                          storage.LocalFileStore)

    def test_invalid_json_falls_back_to_local(self, tmp_path, monkeypatch):
        """Une configuration cassée dégrade, elle ne bloque pas."""
        monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", "{pas du json")
        assert isinstance(storage.make_store(str(tmp_path / "l.json")),
                          storage.LocalFileStore)

    def test_json_without_project_id_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", json.dumps({"type": "service_account"}))
        assert isinstance(storage.make_store(str(tmp_path / "l.json")),
                          storage.LocalFileStore)

    def test_an_explicit_path_stays_local(self, tmp_path):
        """Tests et développement local ne doivent jamais viser une base externe."""
        led = PredictionLedger(path=tmp_path / "l.json")
        assert led.storage_label == "fichier local"


class TestFirestoreStore:
    class _Reponse:
        def __init__(self, code, payload=None):
            self.status_code, self._payload = code, payload or {}

        def json(self):
            return self._payload

    @staticmethod
    def _store(monkeypatch, reponse=None):
        class FausseCred:
            valid = True
            token = "jeton"

        store = storage.FirestoreStore("projet-test", FausseCred())
        # On court-circuite la signature du jeton : ces tests portent sur le
        # dialogue avec Firestore, pas sur la bibliothèque d'authentification.
        monkeypatch.setattr(store, "_headers", lambda: {"Authorization": "Bearer jeton"})
        return store

    def test_a_missing_document_reads_as_empty(self, monkeypatch):
        """Au premier lancement le document n'existe pas encore : ce n'est pas une panne."""
        import requests
        store = self._store(monkeypatch)
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._Reponse(404))
        assert store.load() == []

    def test_entries_are_read_from_the_json_field(self, monkeypatch):
        import requests
        store = self._store(monkeypatch)
        charge = {"fields": {"entries": {"stringValue": json.dumps([{"id": "x"}])}}}
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._Reponse(200, charge))
        assert store.load() == [{"id": "x"}]

    def test_a_network_failure_reads_as_empty_without_raising(self, monkeypatch):
        import requests

        def casse(*_a, **_k):
            raise requests.ConnectionError("panne")

        store = self._store(monkeypatch)
        monkeypatch.setattr(requests, "get", casse)
        assert store.load() == []

    def test_save_sends_the_capped_list(self, monkeypatch):
        import requests
        envoye = {}

        def patch(url, headers=None, json=None, timeout=None):
            envoye["corps"] = json
            return self._Reponse(200)

        store = self._store(monkeypatch)
        store.limit = 2
        monkeypatch.setattr(requests, "patch", patch)
        store.save([{"n": 1}, {"n": 2}, {"n": 3}])
        lignes = json.loads(envoye["corps"]["fields"]["entries"]["stringValue"])
        assert [r["n"] for r in lignes] == [2, 3]

    def test_a_write_failure_does_not_raise(self, monkeypatch):
        import requests

        def casse(*_a, **_k):
            raise requests.ConnectionError("panne")

        store = self._store(monkeypatch)
        monkeypatch.setattr(requests, "patch", casse)
        store.save([{"n": 1}])          # ne doit pas lever


class TestFallbackReason:
    """Un repli muet est introuvable : chaque cause doit se nommer.

    Aucun de ces messages ne doit contenir de valeur secrète — ils
    s'affichent dans l'interface.
    """

    def test_nothing_configured_stays_silent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT", raising=False)
        storage.make_store(str(tmp_path / "l.json"))
        assert storage.FALLBACK_REASON is None

    def test_code_fences_are_named_explicitly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", '```toml\n{"a":1}\n```')
        storage.make_store(str(tmp_path / "l.json"))
        assert "accents graves" in (storage.FALLBACK_REASON or "")

    def test_truncated_json_is_named(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", '{"type":"service_account"')
        storage.make_store(str(tmp_path / "l.json"))
        assert "JSON valide" in (storage.FALLBACK_REASON or "")

    def test_missing_project_id_is_named(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", json.dumps({"type": "service_account"}))
        storage.make_store(str(tmp_path / "l.json"))
        assert "project_id" in (storage.FALLBACK_REASON or "")

    def test_the_reason_never_leaks_the_secret(self, tmp_path, monkeypatch):
        secret = "CLE-PRIVEE-TRES-SECRETE-0123456789"
        monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", json.dumps({
            "type": "service_account", "project_id": "p",
            "private_key": secret, "client_email": "a@b.c",
            "token_uri": "https://oauth2.googleapis.com/token",
        }))
        storage.make_store(str(tmp_path / "l.json"))
        raison = storage.FALLBACK_REASON or ""
        assert raison, "un compte de service refusé doit être expliqué"
        assert secret not in raison
        assert secret[:10] not in raison


class TestLectureFiable:
    """Distinguer « journal vide » de « journal illisible ».

    Le journal s'enregistre en réécrivant le document entier. Confondre les
    deux fait repartir d'une liste vide, puis écrase l'historique avec la
    seule entrée nouvelle — une seconde de réseau coupé suffisait.
    """

    def test_missing_file_is_genuinely_empty(self, tmp_path):
        store = storage.LocalFileStore(str(tmp_path / "jamais-cree.json"))
        assert store.load() == []
        assert store.lecture_fiable, "un premier démarrage n'est pas une panne"

    def test_unreadable_file_is_not_empty(self, tmp_path):
        chemin = tmp_path / "l.json"
        chemin.write_text("ceci n'est pas du json", encoding="utf-8")
        store = storage.LocalFileStore(str(chemin))
        assert store.load() == []
        assert not store.lecture_fiable

    def test_a_failed_read_blocks_the_write(self):
        """Le cas qui détruisait l'historique : lecture ratée, puis écriture."""
        journal = PredictionLedger(store=_StorePanne())
        with pytest.raises(storage.JournalIndisponible):
            journal.record(_Decision(), _Prediction(), "Test")
        assert journal._store.ecrit is None, "rien ne doit être écrit"

    def test_a_failed_read_blocks_resolve_and_annotate(self):
        journal = PredictionLedger(store=_StorePanne())
        with pytest.raises(storage.JournalIndisponible):
            journal.resolve("un-id", 1, 0)
        with pytest.raises(storage.JournalIndisponible):
            journal.annotate("un-id", window="J-1")

    def test_a_healthy_read_still_writes(self, tmp_path):
        journal = PredictionLedger(path=str(tmp_path / "l.json"))
        journal.record(_Decision(), _Prediction(), "Test")
        assert len(journal.all()) == 1


class _StorePanne:
    """Stockage dont la lecture échoue — Firestore injoignable, disque occupé."""

    label = "stockage en panne"

    def __init__(self):
        self.lecture_fiable = True
        self.ecrit = None

    def load(self) -> list[dict]:
        self.lecture_fiable = False
        return []

    def save(self, rows) -> None:
        self.ecrit = rows


class _Prediction:
    sport = "football"
    home = "Alpha"
    away = "Beta"
    outcome_probs: dict[str, float] = {}
    provenances: list = []
    main_pick = type("_Pick", (), {"key": "h2h_home"})()


class _Decision:
    recommendation = "Alpha"
    probability = 0.6
    confidence = 7.0
    odds = 1.8
    fingerprint = "abc"
