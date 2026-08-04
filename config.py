"""Configuration centrale de PronoStat.

Deux points d'entrée uniques :

  * `SUPPORTED_COMPETITIONS` — la couverture. Ajouter une compétition ne
    demande AUCUNE modification du moteur ni de la collecte.
  * `SOURCE_RELIABILITY` — la confiance accordée à chaque source, utilisée par
    le moteur de recherche approfondie pour arbitrer les désaccords.

Les clés API sont lues depuis l'environnement ET depuis les « secrets »
Streamlit, ce qui rend le projet directement déployable en ligne.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

try:  # python-dotenv est optionnel (absent en déploiement, présent en local)
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dépendance absente
    pass


ROOT = Path(__file__).resolve().parent
CACHE_DIR = Path(os.getenv("PRONOSTAT_CACHE_DIR", ROOT / ".cache"))
try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:  # système de fichiers en lecture seule (certains hébergeurs)
    CACHE_DIR = Path(os.getenv("TMPDIR", "/tmp")) / "pronostat-cache"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

HTTP_CACHE_FILE = CACHE_DIR / "http_cache.json"
QUOTA_FILE = CACHE_DIR / "quota.json"
HISTORY_FILE = CACHE_DIR / "history.json"
ODDS_HISTORY_FILE = CACHE_DIR / "odds_history.json"


# --------------------------------------------------------------------------
# Lecture des réglages : variables d'environnement + secrets Streamlit
# --------------------------------------------------------------------------
# Renseigné si le fichier de secrets existe mais refuse d'être lu. Une seule
# cause en pratique : du TOML invalide — le plus souvent les délimiteurs de
# bloc de code (```toml) collés par mégarde avec le contenu.
SECRETS_ERROR: str | None = None

# Absence de fichier de secrets : cas parfaitement normal en local, où la
# configuration vient de `.env`. À distinguer d'un fichier illisible.
_SECRETS_ABSENT = ("no secrets", "not found", "does not exist", "st.secrets has no")


def _secret(name: str, default: str = "") -> str:
    """Valeur de configuration, quelle que soit la façon dont elle est fournie.

    Ordre : variable d'environnement (local, Docker) → secrets Streamlit
    (déploiement en ligne) → valeur par défaut. L'import de Streamlit est
    volontairement paresseux et protégé pour que les tests n'en dépendent pas.

    Un fichier de secrets mal formé est retenu dans `SECRETS_ERROR` au lieu
    d'être ignoré : sans cela, *toutes* les clés retombent sur leurs valeurs
    par défaut et l'application se comporte comme si rien n'avait été
    configuré, sans afficher la moindre explication.
    """
    global SECRETS_ERROR
    value = os.getenv(name)
    if value not in (None, ""):
        return value
    try:  # pragma: no cover - dépend de l'environnement d'exécution
        import streamlit as st

        if name in st.secrets:
            return str(st.secrets[name])
    except Exception as exc:  # pragma: no cover - idem
        message = str(exc)
        if not any(motif in message.lower() for motif in _SECRETS_ABSENT):
            SECRETS_ERROR = f"{type(exc).__name__} : {message}"[:300]
    return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _secret(name)
    if raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "oui"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(_secret(name) or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_secret(name) or default)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# Sports gérés
# --------------------------------------------------------------------------
SPORTS = {
    "football": {"label": "Football", "icon": "⚽"},
    "basket": {"label": "Basket", "icon": "🏀"},
    "tennis": {"label": "Tennis", "icon": "🎾"},
    "hockey": {"label": "Hockey sur glace", "icon": "🏒"},
}


def european_season(today: date | None = None) -> int:
    """Année de départ de la saison européenne en cours (2025 pour 2025-26)."""
    today = today or datetime.now(timezone.utc).date()
    return today.year if today.month >= 7 else today.year - 1


def season_label(start_year: int | None = None) -> str:
    """Libellé de saison façon Wikipédia : « 2025–26 » (tiret demi-cadratin)."""
    year = start_year or european_season()
    return f"{year}–{str(year + 1)[2:]}"


def openfootball_season(start_year: int | None = None) -> str:
    """Libellé de saison façon openfootball : « 2025-26 »."""
    year = start_year or european_season()
    return f"{year}-{str(year + 1)[2:]}"


# ==========================================================================
#  REGISTRE DES COMPÉTITIONS
# ==========================================================================
@dataclass(frozen=True)
class Competition:
    """Une compétition couverte, reliée aux identifiants de chaque source.

    Tous les champs de source sont optionnels : une compétition reste
    utilisable même si une seule source la connaît. Plus il y en a, plus le
    moteur de recherche approfondie peut recouper et fiabiliser les données.
    """

    key: str                    # identifiant interne stable
    label: str                  # libellé affiché dans l'interface
    sport: str
    tier: int = 1               # 1 = compétition majeure, 2 = secondaire

    # --- identifiants par source ---
    odds_key: str | None = None            # clé The Odds API
    odds_patterns: tuple[str, ...] = ()    # résolution dynamique (tennis)
    odds_title: str | None = None          # repli par titre si la clé a changé
    sportsdb_league: str | None = None     # nom de ligue TheSportsDB
    football_data_code: str | None = None  # code football-data.org
    api_football_id: int | None = None     # identifiant API-Football
    openfootball_code: str | None = None   # code openfootball (« en.1 »)
    footballdata_code: str | None = None   # code football-data.co.uk (« E0 »)
    wikipedia_page: str | None = None      # titre de la page de saison
    wikidata_qid: str | None = None        # identifiant Wikidata de la ligue
    nhl: bool = False                      # utilise l'API publique NHL
    balldontlie: bool = False              # utilise balldontlie (NBA)

    # --- comportement ---
    is_cup: bool = False        # coupe : pas de classement, format à élimination
    team_pool: tuple[str, ...] = ()        # coupes : viviers d'équipes en repli
    expected_teams: int = 0     # effectif attendu (0 = inconnu / variable)
    enabled: bool = True

    @property
    def scope(self) -> str:
        """Espace de nommage du cache : une compétition, un compartiment."""
        return f"{self.sport}:{self.key}"

    def wikipedia_title(self, start_year: int | None = None) -> str | None:
        """Titre de la page Wikipédia de la saison en cours."""
        if not self.wikipedia_page:
            return None
        return f"{season_label(start_year)} {self.wikipedia_page}"


# Les coupes nationales ne sont proposées que si vous les activez : les
# sources gratuites y sont nettement moins fiables (compos, stats partielles).
_CUPS = _env_bool("COMPETITIONS_INCLUDE_NATIONAL_CUPS", False)

_BIG5 = ("premier_league", "la_liga", "bundesliga", "serie_a", "ligue_1")

SUPPORTED_COMPETITIONS: dict[str, tuple[Competition, ...]] = {
    # ------------------------------------------------------------------
    # FOOTBALL — priorité principale de l'application
    # ------------------------------------------------------------------
    "football": (
        Competition(
            key="premier_league", label="Premier League", sport="football",
            odds_key="soccer_epl", odds_title="EPL",
            sportsdb_league="English Premier League",
            football_data_code="PL", api_football_id=39,
            openfootball_code="en.1", footballdata_code="E0", wikipedia_page="Premier League",
            wikidata_qid="Q9448", expected_teams=20,
        ),
        Competition(
            key="la_liga", label="La Liga", sport="football",
            odds_key="soccer_spain_la_liga", odds_title="La Liga - Spain",
            sportsdb_league="Spanish La Liga",
            football_data_code="PD", api_football_id=140,
            openfootball_code="es.1", footballdata_code="SP1", wikipedia_page="La Liga",
            wikidata_qid="Q324867", expected_teams=20,
        ),
        Competition(
            key="bundesliga", label="Bundesliga", sport="football",
            odds_key="soccer_germany_bundesliga", odds_title="Bundesliga - Germany",
            sportsdb_league="German Bundesliga",
            football_data_code="BL1", api_football_id=78,
            openfootball_code="de.1", footballdata_code="D1", wikipedia_page="Bundesliga",
            wikidata_qid="Q82595", expected_teams=18,
        ),
        Competition(
            key="serie_a", label="Serie A", sport="football",
            odds_key="soccer_italy_serie_a", odds_title="Serie A - Italy",
            sportsdb_league="Italian Serie A",
            football_data_code="SA", api_football_id=135,
            openfootball_code="it.1", footballdata_code="I1", wikipedia_page="Serie A",
            wikidata_qid="Q15804", expected_teams=20,
        ),
        Competition(
            key="ligue_1", label="Ligue 1", sport="football",
            odds_key="soccer_france_ligue_one", odds_title="Ligue 1 - France",
            sportsdb_league="French Ligue 1",
            football_data_code="FL1", api_football_id=61,
            openfootball_code="fr.1", footballdata_code="F1", wikipedia_page="Ligue 1",
            wikidata_qid="Q13394", expected_teams=18,
        ),
        Competition(
            key="ucl", label="UEFA Champions League", sport="football",
            odds_key="soccer_uefa_champs_league", odds_title="UEFA Champions League",
            sportsdb_league="UEFA Champions League",
            football_data_code="CL", api_football_id=2,
            wikipedia_page="UEFA Champions League",
            is_cup=True, team_pool=_BIG5, expected_teams=36,
        ),
        Competition(
            key="uel", label="UEFA Europa League", sport="football",
            odds_key="soccer_uefa_europa_league", odds_title="UEFA Europa League",
            sportsdb_league="UEFA Europa League",
            api_football_id=3, wikipedia_page="UEFA Europa League",
            is_cup=True, team_pool=_BIG5, expected_teams=36,
        ),
        Competition(
            key="uecl", label="UEFA Conference League", sport="football",
            odds_key="soccer_uefa_europa_conference_league",
            odds_title="UEFA Europa Conference League",
            sportsdb_league="UEFA Conference League",
            api_football_id=848, wikipedia_page="UEFA Conference League",
            is_cup=True, team_pool=_BIG5, expected_teams=36,
        ),
        Competition(
            key="uefa_super_cup", label="Supercoupe d'Europe", sport="football",
            tier=2, odds_title="UEFA Super Cup",
            sportsdb_league="UEFA Super Cup", api_football_id=531,
            is_cup=True, team_pool=_BIG5, expected_teams=2,
        ),
        # --- coupes nationales (désactivées par défaut) ---
        Competition(
            key="fa_cup", label="FA Cup", sport="football", tier=2,
            odds_key="soccer_fa_cup", odds_title="FA Cup",
            sportsdb_league="English FA Cup", api_football_id=45,
            is_cup=True, team_pool=("premier_league",), enabled=_CUPS,
        ),
        Competition(
            key="copa_del_rey", label="Copa del Rey", sport="football", tier=2,
            odds_key="soccer_spain_copa_del_rey", odds_title="Copa del Rey",
            sportsdb_league="Spanish Copa del Rey", api_football_id=143,
            is_cup=True, team_pool=("la_liga",), enabled=_CUPS,
        ),
        Competition(
            key="dfb_pokal", label="DFB-Pokal", sport="football", tier=2,
            odds_key="soccer_germany_dfb_pokal", odds_title="DFB Pokal",
            sportsdb_league="German DFB Pokal", api_football_id=81,
            is_cup=True, team_pool=("bundesliga",), enabled=_CUPS,
        ),
        Competition(
            key="coppa_italia", label="Coppa Italia", sport="football", tier=2,
            odds_key="soccer_italy_coppa_italia", odds_title="Coppa Italia",
            sportsdb_league="Italian Coppa Italia", api_football_id=137,
            is_cup=True, team_pool=("serie_a",), enabled=_CUPS,
        ),
        Competition(
            key="coupe_de_france", label="Coupe de France", sport="football", tier=2,
            odds_title="Coupe de France",
            sportsdb_league="French Coupe de France", api_football_id=66,
            is_cup=True, team_pool=("ligue_1",), enabled=_CUPS,
        ),
    ),
    # ------------------------------------------------------------------
    # BASKET — compétitions américaines majeures uniquement
    # ------------------------------------------------------------------
    "basket": (
        Competition(
            key="nba", label="NBA", sport="basket",
            odds_key="basketball_nba", odds_title="NBA",
            sportsdb_league="NBA", balldontlie=True,
            wikidata_qid="Q155223", expected_teams=30,
        ),
        Competition(
            key="wnba", label="WNBA", sport="basket",
            odds_key="basketball_wnba", odds_title="WNBA",
            sportsdb_league="WNBA", wikidata_qid="Q2593221", expected_teams=15,
        ),
        Competition(
            key="nba_summer_league", label="NBA Summer League", sport="basket", tier=2,
            odds_title="NBA Summer League", sportsdb_league="NBA Summer League",
            enabled=_env_bool("COMPETITIONS_INCLUDE_SUMMER_LEAGUE", False),
        ),
        Competition(
            key="nba_g_league", label="NBA G League", sport="basket", tier=2,
            odds_title="NBA G League", sportsdb_league="NBA G League",
            enabled=_env_bool("COMPETITIONS_INCLUDE_G_LEAGUE", False),
        ),
    ),
    # ------------------------------------------------------------------
    # HOCKEY — NHL uniquement
    # ------------------------------------------------------------------
    "hockey": (
        Competition(
            key="nhl", label="NHL", sport="hockey",
            odds_key="icehockey_nhl", odds_title="NHL",
            sportsdb_league="NHL", nhl=True,
            wikidata_qid="Q1215892", expected_teams=32,
        ),
    ),
    # ------------------------------------------------------------------
    # TENNIS — circuits professionnels majeurs
    # Les tournois changent chaque semaine : les clés The Odds API sont
    # résolues dynamiquement par motif, jamais codées en dur.
    # ------------------------------------------------------------------
    "tennis": (
        Competition(
            key="atp_grand_slam", label="ATP · Grand Chelem", sport="tennis",
            odds_patterns=(r"^tennis_atp_(aus(tralian)?_open|french_open|"
                           r"roland_garros|wimbledon|us_open)$",),
            is_cup=True,
        ),
        Competition(
            key="wta_grand_slam", label="WTA · Grand Chelem", sport="tennis",
            odds_patterns=(r"^tennis_wta_(aus(tralian)?_open|french_open|"
                           r"roland_garros|wimbledon|us_open)$",),
            is_cup=True,
        ),
        Competition(
            key="atp_tour", label="ATP Tour (Masters 1000 / 500 / 250)", sport="tennis",
            odds_patterns=(r"^tennis_atp_(?!.*(aus(tralian)?_open|french_open|"
                           r"roland_garros|wimbledon|us_open|finals|davis_cup)).*$",),
            is_cup=True,
        ),
        Competition(
            key="wta_tour", label="WTA Tour (1000 / 500 / 250)", sport="tennis",
            odds_patterns=(r"^tennis_wta_(?!.*(aus(tralian)?_open|french_open|"
                           r"roland_garros|wimbledon|us_open|finals|"
                           r"billie_jean)).*$",),
            is_cup=True,
        ),
        Competition(
            key="atp_finals", label="ATP Finals", sport="tennis",
            odds_patterns=(r"^tennis_atp_.*finals$",), is_cup=True,
        ),
        Competition(
            key="wta_finals", label="WTA Finals", sport="tennis",
            odds_patterns=(r"^tennis_wta_.*finals$",), is_cup=True,
        ),
        Competition(
            key="davis_cup", label="Coupe Davis", sport="tennis",
            odds_patterns=(r"^tennis_atp_davis_cup$", r"^tennis_.*davis.*$"),
            is_cup=True,
        ),
        Competition(
            key="bjk_cup", label="Billie Jean King Cup", sport="tennis",
            odds_patterns=(r"^tennis_.*billie_jean.*$", r"^tennis_.*fed_cup$"),
            is_cup=True,
        ),
    ),
}

# Circuits explicitement exclus : jamais proposés, même si l'API les liste.
TENNIS_EXCLUDED_PATTERN = r"(itf|challenger|futures|exhibition|utr)"


def competitions(sport: str, include_disabled: bool = False) -> list[Competition]:
    comps = SUPPORTED_COMPETITIONS.get(sport, ())
    if include_disabled:
        return list(comps)
    return [c for c in comps if c.enabled]


def competition(sport: str, key: str) -> Competition | None:
    for comp in SUPPORTED_COMPETITIONS.get(sport, ()):
        if comp.key == key:
            return comp
    return None


def all_competitions(include_disabled: bool = False) -> list[Competition]:
    out: list[Competition] = []
    for sport in SUPPORTED_COMPETITIONS:
        out.extend(competitions(sport, include_disabled))
    return out


# ==========================================================================
#  SOURCES : clés, activation, fiabilité
# ==========================================================================
@dataclass(frozen=True)
class ApiKeys:
    odds_api: str = ""
    football_data: str = ""
    rapidapi: str = ""
    balldontlie: str = ""
    thesportsdb: str = "3"

    @classmethod
    def from_environment(cls) -> "ApiKeys":
        return cls(
            odds_api=_secret("ODDS_API_KEY").strip(),
            football_data=_secret("FOOTBALL_DATA_API_KEY").strip(),
            rapidapi=_secret("RAPIDAPI_KEY").strip(),
            balldontlie=_secret("BALLDONTLIE_API_KEY").strip(),
            thesportsdb=_secret("THESPORTSDB_API_KEY", "3").strip() or "3",
        )


KEYS = ApiKeys.from_environment()


def secrets_report() -> dict[str, object]:
    """État de la configuration, sans jamais révéler une seule valeur.

    Quand aucune clé n'est active en ligne, la cause est invisible : les
    valeurs par défaut prennent le relais en silence. Ce rapport dit
    précisément *où* la lecture échoue — fichier absent, illisible, ou
    présent mais ne contenant pas les clés attendues.

    Ne sont exposés que des **noms** et des **longueurs**. Aucune valeur, ni
    même un fragment, ne sort d'ici : ce rapport s'affiche dans l'interface.
    """
    attendues = [
        "ODDS_API_KEY", "FOOTBALL_DATA_API_KEY", "RAPIDAPI_KEY",
        "BALLDONTLIE_API_KEY", "THESPORTSDB_API_KEY", "PRONOSTAT_ENV",
    ]
    rapport: dict[str, object] = {
        "streamlit_importable": False,
        "secrets_lisibles": False,
        "erreur": None,
        "noms_dans_secrets": [],
        "origine": {},
    }

    noms_secrets: list[str] = []
    try:
        import streamlit as st

        rapport["streamlit_importable"] = True
        noms_secrets = sorted(str(k) for k in st.secrets.keys())
        rapport["secrets_lisibles"] = True
        rapport["noms_dans_secrets"] = noms_secrets
    except Exception as exc:
        rapport["erreur"] = f"{type(exc).__name__} : {str(exc)[:200]}"

    for nom in attendues:
        depuis_env = os.getenv(nom)
        if depuis_env not in (None, ""):
            rapport["origine"][nom] = f"variable d'environnement ({len(depuis_env)} car.)"
        elif nom in noms_secrets:
            longueur = len(str(_secret(nom)))
            rapport["origine"][nom] = f"secrets Streamlit ({longueur} car.)"
        else:
            rapport["origine"][nom] = "ABSENTE"
    return rapport


@dataclass(frozen=True)
class SourceToggles:
    the_odds_api: bool = _env_bool("SOURCE_THE_ODDS_API", True)
    football_data: bool = _env_bool("SOURCE_FOOTBALL_DATA", True)
    api_football: bool = _env_bool("SOURCE_API_FOOTBALL", True)
    thesportsdb: bool = _env_bool("SOURCE_THESPORTSDB", True)
    balldontlie: bool = _env_bool("SOURCE_BALLDONTLIE", True)
    nhl_api: bool = _env_bool("SOURCE_NHL_API", True)
    openfootball: bool = _env_bool("SOURCE_OPENFOOTBALL", True)
    footballdata_uk: bool = _env_bool("SOURCE_FOOTBALLDATA_UK", True)
    wikipedia: bool = _env_bool("SOURCE_WIKIPEDIA", True)
    wikidata: bool = _env_bool("SOURCE_WIKIDATA", True)
    news_rss: bool = _env_bool("SOURCE_NEWS_RSS", True)
    weather: bool = _env_bool("SOURCE_WEATHER", True)


SOURCES = SourceToggles()

PREMIUM_MODE = _env_bool("PRONOSTAT_PREMIUM", False)

# Fiabilité de chaque source, entre 0 et 1. Le moteur de recherche
# approfondie s'en sert pour arbitrer quand deux sources se contredisent, et
# pour calculer l'indice de fiabilité affiché à l'utilisateur.
#   ≥ 0.90 : API officielle de la ligue ou opérateur de marché
#   ≈ 0.80 : donnée publique structurée, mise à jour régulièrement
#   ≈ 0.55 : palier gratuit bridé, données partielles
#   ≤ 0.40 : signal indicatif, jamais utilisé dans les calculs
SOURCE_RELIABILITY: dict[str, float] = {
    "nhl_api": 0.96,
    "the_odds_api": 0.95,
    "api_football": 0.92,
    "football_data": 0.90,
    "balldontlie": 0.88,
    "openfootball": 0.85,
    "football_data_uk": 0.87,
    "wikipedia": 0.80,
    "wikidata": 0.75,
    "open_meteo": 0.70,
    "thesportsdb": 0.55,
    "news_rss": 0.40,
}
DEFAULT_RELIABILITY = 0.50


def reliability(source: str) -> float:
    return SOURCE_RELIABILITY.get(source, DEFAULT_RELIABILITY)


# Noms « grand public » des sources : l'interface ne montre jamais de nom
# technique d'API (§6). Le code et la documentation les gardent.
SOURCE_PUBLIC_NAMES: dict[str, str] = {
    "the_odds_api": "Cotes des bookmakers",
    "api_football": "Statistiques officielles du championnat",
    "football_data": "Résultats officiels du championnat",
    "balldontlie": "Données officielles NBA",
    "nhl_api": "Données officielles NHL",
    "openfootball": "Calendriers officiels des championnats",
    "football_data_uk": "Archives de résultats et de cotes",
    "wikipedia": "Encyclopédie sportive",
    "wikidata": "Base de connaissances sportive",
    "thesportsdb": "Base sportive publique",
    "news_rss": "Actualité sportive",
    "open_meteo": "Prévisions météo",
}


def public_name(source: str) -> str:
    return SOURCE_PUBLIC_NAMES.get(source, "Source sportive")


# ==========================================================================
#  Quotas
# ==========================================================================
@dataclass(frozen=True)
class QuotaRule:
    provider: str
    label: str          # libellé grand public affiché dans l'interface
    limit: int
    period: str         # "day" | "month"


QUOTAS: tuple[QuotaRule, ...] = (
    QuotaRule("the_odds_api", "Cotes des bookmakers",
              _env_int("QUOTA_ODDS_MONTH", 500), "month"),
    QuotaRule("api_football", "Statistiques du championnat",
              _env_int("QUOTA_API_FOOTBALL_DAY", 100), "day"),
    QuotaRule("football_data", "Résultats du championnat",
              _env_int("QUOTA_FOOTBALL_DATA_DAY", 100), "day"),
    QuotaRule("balldontlie", "Données NBA",
              _env_int("QUOTA_BALLDONTLIE_DAY", 300), "day"),
)

QUOTA_WARN_RATIO = _env_float("QUOTA_WARN_RATIO", 0.80)


# ==========================================================================
#  Cache et réseau
# ==========================================================================
@dataclass(frozen=True)
class Ttl:
    roster: int = _env_int("TTL_ROSTER", 7 * 24 * 3600)   # effectifs : très stables
    catalog: int = _env_int("TTL_CATALOG", 12 * 3600)
    events: int = _env_int("TTL_EVENTS", 3 * 3600)
    odds: int = _env_int("TTL_ODDS", 20 * 60)
    form: int = _env_int("TTL_FORM", 12 * 3600)
    standings: int = _env_int("TTL_STANDINGS", 12 * 3600)
    stats: int = _env_int("TTL_STATS", 30 * 24 * 3600)    # match passé : figé
    news: int = _env_int("TTL_NEWS", 3600)
    weather: int = _env_int("TTL_WEATHER", 3 * 3600)


TTL = Ttl()

HTTP_TIMEOUT = _env_float("HTTP_TIMEOUT", 12.0)
# Reprises après une erreur réseau. Une coupure passagère suffisait à faire
# perdre les cotes d'un match, et donc à effondrer la confiance de l'analyse.
# Une requête qui n'aboutit pas n'est pas facturée : ces essais ne coûtent
# aucun crédit d'API.
HTTP_RETRIES = _env_int("HTTP_RETRIES", 2)
HTTP_RETRY_DELAY = _env_float("HTTP_RETRY_DELAY", 0.6)
# Le service SPARQL public de Wikidata est plus lent qu'une API REST. Il
# n'est qu'un complément : s'il n'a pas répondu à temps, les autres sources
# suffisent, donc on ne l'attend pas indéfiniment.
SPARQL_TIMEOUT = _env_float("SPARQL_TIMEOUT", 25.0)
STALE_AFTER = _env_int("STALE_AFTER", 48 * 3600)

# Nombre d'appels réseau menés en parallèle par le moteur de collecte.
MAX_PARALLEL_FETCHES = max(1, _env_int("MAX_PARALLEL_FETCHES", 8))

# Identité déclarée aux API publiques (exigée par MediaWiki et Wikidata).
USER_AGENT = _secret(
    "PRONOSTAT_USER_AGENT",
    "PronoStat/3.0 (application locale d'analyse sportive)",
)


# ==========================================================================
#  Paramètres du moteur d'analyse
# ==========================================================================
@dataclass(frozen=True)
class EngineConfig:
    market_weight: float = _env_float("MARKET_WEIGHT", 0.60)
    # Part du poids « marché » accordée au repère tiré des cotes de la saison,
    # quand aucune cote du match n'est disponible. Un repère de saison décrit
    # les équipes, pas la rencontre : il pèse donc moins qu'une vraie cote.
    reference_anchor_ratio: float = _env_float("REFERENCE_ANCHOR_RATIO", 0.55)
    n_sims: int = max(10_000, _env_int("MC_SIMS", 20_000))
    value_threshold: float = _env_float("VALUE_THRESHOLD", 0.05)
    home_advantage_football: float = _env_float("HOME_ADV_FOOTBALL", 1.12)
    home_advantage_hockey: float = _env_float("HOME_ADV_HOCKEY", 1.06)
    home_advantage_basket: float = _env_float("HOME_ADV_BASKET", 2.4)
    dixon_coles_rho: float = _env_float("DIXON_COLES_RHO", -0.08)
    max_goals: int = _env_int("MAX_GOALS", 12)
    min_matches: int = _env_int("MIN_MATCHES", 4)
    seed: int | None = None
    league_avg_goals_football: float = _env_float("LEAGUE_AVG_GOALS_FOOTBALL", 1.40)
    league_avg_goals_hockey: float = _env_float("LEAGUE_AVG_GOALS_HOCKEY", 3.05)
    league_avg_points_basket: float = _env_float("LEAGUE_AVG_POINTS_BASKET", 113.0)
    basket_points_sd: float = _env_float("BASKET_POINTS_SD", 11.5)
    basket_corr: float = _env_float("BASKET_CORR", 0.32)

    # --- signaux enrichis : chacun neutralisable avec 0 ---
    xg_weight: float = _env_float("XG_WEIGHT", 0.50)
    h2h_weight: float = _env_float("H2H_WEIGHT", 0.15)
    standings_weight: float = _env_float("STANDINGS_WEIGHT", 0.20)
    rest_penalty_max: float = _env_float("REST_PENALTY_MAX", 0.04)
    rest_reference_days: float = _env_float("REST_REFERENCE_DAYS", 3.0)

    # --- choix du pronostic principal ---
    # Fourchette de probabilité exploitable. En dessous, le pronostic est trop
    # incertain ; au-dessus, il est quasi certain donc sans intérêt (une issue
    # à 90 % se paie 1,11 : la recommander n'apprend rien).
    pick_min_probability: float = _env_float("PICK_MIN_PROBABILITY", 0.45)
    pick_max_probability: float = _env_float("PICK_MAX_PROBABILITY", 0.80)
    # Bonus accordé au marché « vainqueur », qui reste la lecture naturelle.
    pick_winner_bonus: float = _env_float("PICK_WINNER_BONUS", 0.10)


ENGINE = EngineConfig()


# ==========================================================================
#  AGENT D'ANALYSE — pondérations du raisonnement multicritère
# ==========================================================================
# Poids relatifs des facteurs pris en compte par l'agent. Ils sont
# volontairement externalisés : ce sont eux qu'on ajuste au fil du temps,
# à la lumière des statistiques de calibration (voir agent/memory.py).
#
# `in_model=True` signale un facteur DÉJÀ consommé par la simulation
# (forces d'équipe, calibration…). Il est alors affiché comme facteur
# explicatif mais n'est PAS réappliqué à la décision : le compter deux fois
# fausserait la probabilité.
@dataclass(frozen=True)
class FactorSpec:
    key: str
    label: str          # libellé grand public
    weight: float
    in_model: bool      # déjà pris en compte par la simulation ?


def _w(name: str, default: float) -> float:
    return _env_float(f"WEIGHT_{name.upper()}", default)


FACTOR_WEIGHTS: tuple[FactorSpec, ...] = (
    # --- facteurs déjà intégrés à la simulation (explicatifs) ---
    FactorSpec("forme_recente", "Forme récente", _w("forme_recente", 1.00), True),
    FactorSpec("confrontations", "Confrontations directes", _w("confrontations", 0.45), True),
    FactorSpec("domicile_exterieur", "Avantage du terrain", _w("domicile_exterieur", 0.70), True),
    FactorSpec("efficacite_offensive", "Efficacité offensive", _w("efficacite_offensive", 0.85), True),
    FactorSpec("solidite_defensive", "Solidité défensive", _w("solidite_defensive", 0.85), True),
    FactorSpec("classement", "Position au classement", _w("classement", 0.60), True),
    FactorSpec("recuperation", "Temps de récupération", _w("recuperation", 0.35), True),
    FactorSpec("consensus_marche", "Consensus des bookmakers", _w("consensus_marche", 1.20), True),
    # --- facteurs NON consommés par la simulation (ils ajustent la décision) ---
    FactorSpec("dynamique", "Dynamique (séries en cours)", _w("dynamique", 0.40), False),
    FactorSpec("calendrier", "Charge de calendrier", _w("calendrier", 0.30), False),
    FactorSpec("importance", "Enjeu du match", _w("importance", 0.25), False),
    FactorSpec("evolution_cotes", "Évolution des cotes", _w("evolution_cotes", 0.55), False),
)

# Facteurs de contexte : ils ne penchent pour aucune équipe, mais pèsent sur
# la confiance (couverture et qualité des informations réunies).
CONTEXT_FACTORS: tuple[FactorSpec, ...] = (
    FactorSpec("disponibilite_donnees", "Données disponibles", _w("disponibilite_donnees", 1.0), False),
    FactorSpec("qualite_sources", "Qualité des sources", _w("qualite_sources", 1.0), False),
)


@dataclass(frozen=True)
class AgentConfig:
    """Réglages du raisonnement de l'agent."""

    # Amplitude maximale de l'ajustement issu des facteurs hors simulation.
    # Volontairement faible : le consensus du marché doit rester dominant.
    tilt_strength: float = _env_float("AGENT_TILT_STRENGTH", 0.08)
    # Nombre de lots indépendants servant à mesurer la stabilité des
    # probabilités (auto-évaluation). Plus il est élevé, plus la mesure est
    # fine, mais chaque lot est plus petit donc plus bruité.
    stability_batches: int = max(2, _env_int("AGENT_STABILITY_BATCHES", 5))
    # Réduction maximale de confiance provoquée par les contradictions.
    contradiction_penalty_max: float = _env_float("AGENT_CONTRADICTION_PENALTY", 2.5)
    # Nombre de facteurs clés et de risques listés dans la décision.
    top_factors: int = max(1, _env_int("AGENT_TOP_FACTORS", 4))
    top_risks: int = max(1, _env_int("AGENT_TOP_RISKS", 3))
    # Analyse reproductible : la graine dérive des données, pas de l'horloge.
    deterministic: bool = _env_bool("AGENT_DETERMINISTIC", True)
    # Seuil d'écart modèle/marché à partir duquel on parle de contradiction.
    market_divergence_threshold: float = _env_float("AGENT_MARKET_DIVERGENCE", 0.12)
    # Dérive de cote (en relatif) considérée comme un signal fort.
    odds_drift_threshold: float = _env_float("AGENT_ODDS_DRIFT", 0.06)


AGENT = AgentConfig()


# ==========================================================================
#  Environnement d'exécution : développement ou production
# ==========================================================================
ENVIRONMENT = (_secret("PRONOSTAT_ENV", "development") or "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT in {"production", "prod"}

# En production, on ne journalise pas le détail des appels et on n'expose
# jamais les rouages internes dans l'interface.
LOG_LEVEL = _secret("PRONOSTAT_LOG_LEVEL", "WARNING" if IS_PRODUCTION else "INFO")
SHOW_INTERNALS = _env_bool("PRONOSTAT_SHOW_INTERNALS", not IS_PRODUCTION)

# Mode diagnostic : destiné au développement. Il expose les rouages internes
# (sources interrogées, données manquantes, valeurs du modèle). Toujours
# désactivé en production, quelle que soit la valeur demandée.
DEBUG_MODE = _env_bool("PRONOSTAT_DEBUG", False) and not IS_PRODUCTION


def configure_logging() -> None:
    """Journalisation cohérente entre l'interface, les sources et l'agent."""
    import logging

    level = logging.DEBUG if DEBUG_MODE else getattr(
        logging, str(LOG_LEVEL).upper(), logging.WARNING
    )
    root = logging.getLogger("pronostat")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s : %(message)s",
                              datefmt="%H:%M:%S")
        )
        root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


TOTALS_LINES = {
    "football": [1.5, 2.5, 3.5],
    "hockey": [4.5, 5.5, 6.5],
    "basket": [],      # la ligne vient du marché (ex. 224.5)
    "tennis": [21.5, 22.5, 23.5],  # jeux
}

FORM_WINDOW = _env_int("FORM_WINDOW", 10)
