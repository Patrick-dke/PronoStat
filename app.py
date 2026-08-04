"""PronoStat — interface Streamlit.

Parcours (§6 de la révision) : sport → compétition → équipe 1 → équipe 2 → bouton.
Restreindre la couverture aux compétitions majeures raccourcit les listes,
accélère le chargement et économise les quotas.

Tout le travail est fait en arrière-plan ; l'écran n'affiche que des résultats
compacts — cartes, jauges, graphiques — jamais de pavés de texte.

Lancement :  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import html
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

import config as cfg
import data_sources as data_sources_module
import engine as engine_module
from agent import AnalysisAgent, AnalysisResult, PerformanceAnalyst, TuningAdvisor
from config import Competition
from data_sources import DataHub, normalize_name
from engine import Prediction

st.set_page_config(
    page_title="PronoStat",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="collapsed",
)

cfg.configure_logging()

# ==========================================================================
# Palette & feuille de style
# ==========================================================================
GOLD = "#D9B45B"
EMERALD = "#2FD3A2"
RED = "#E0685F"
BLUE = "#5B8DEF"
TEXT = "#E8EDF4"
MUTED = "#8A97A8"
CARD = "#141B24"
LINE = "#222D3B"

STYLE = f"""
<style>
  #MainMenu, footer {{visibility: hidden;}}
  .stApp {{
      background:
        radial-gradient(1100px 520px at 12% -8%, #16202C 0%, rgba(11,15,20,0) 60%),
        radial-gradient(900px 460px at 92% 0%, #131E22 0%, rgba(11,15,20,0) 55%),
        #0B0F14;
      color: {TEXT};
  }}
  .block-container {{ padding-top: 1.6rem; padding-bottom: 5rem; max-width: 1220px; }}
  h1, h2, h3, h4 {{ letter-spacing: -0.02em; }}

  /* ---------- en-tête ---------- */
  .ps-header {{ display:flex; align-items:center; justify-content:space-between;
                gap:1rem; flex-wrap:wrap; margin-bottom:1.1rem; }}
  .ps-brand {{ display:flex; align-items:baseline; gap:.7rem; flex-wrap:wrap; }}
  .ps-logo {{ font-size:1.85rem; font-weight:700; color:{TEXT}; letter-spacing:-.03em; }}
  .ps-logo span {{ color:{GOLD}; }}
  .ps-tag {{ color:{MUTED}; font-size:.8rem; letter-spacing:.13em; text-transform:uppercase; }}
  .ps-chips {{ display:flex; gap:.45rem; flex-wrap:wrap; }}

  /* ---------- cartes ---------- */
  .ps-card {{ background:linear-gradient(180deg, #161E28 0%, {CARD} 100%);
              border:1px solid {LINE}; border-radius:16px; padding:1.05rem 1.2rem;
              height:100%; box-shadow:0 1px 0 rgba(255,255,255,.03) inset; }}
  .ps-card-title {{ color:{MUTED}; font-size:.72rem; font-weight:600;
                    letter-spacing:.13em; text-transform:uppercase; margin-bottom:.55rem; }}
  .ps-big {{ font-size:2.05rem; font-weight:700; line-height:1.1; color:{TEXT}; }}
  .ps-mid {{ font-size:1.35rem; font-weight:650; color:{TEXT}; }}
  .ps-sub {{ color:{MUTED}; font-size:.83rem; margin-top:.3rem; }}
  .ps-accent {{ color:{GOLD}; }}

  /* ---------- badges ---------- */
  .ps-badge {{ display:inline-flex; align-items:center; gap:.35rem; font-size:.72rem;
               font-weight:600; padding:.24rem .62rem; border-radius:999px;
               border:1px solid {LINE}; background:#101720; color:{MUTED};
               margin:.1rem .1rem 0 0; }}
  .ps-badge.gold {{ color:{GOLD}; border-color:rgba(217,180,91,.35); background:rgba(217,180,91,.08); }}
  .ps-badge.em {{ color:{EMERALD}; border-color:rgba(47,211,162,.32); background:rgba(47,211,162,.08); }}
  .ps-badge.warn {{ color:{RED}; border-color:rgba(224,104,95,.32); background:rgba(224,104,95,.08); }}

  /* ---------- lignes de marché ---------- */
  .ps-row {{ display:flex; align-items:center; justify-content:space-between;
             padding:.42rem 0; border-bottom:1px dashed rgba(255,255,255,.055); }}
  .ps-row:last-child {{ border-bottom:none; }}
  .ps-row .lbl {{ color:{TEXT}; font-size:.9rem; }}
  .ps-row .val {{ font-variant-numeric:tabular-nums; font-weight:650; font-size:.95rem; }}
  .ps-bar {{ height:6px; border-radius:99px; background:#1B2431; overflow:hidden; margin-top:.3rem; }}
  .ps-bar > i {{ display:block; height:100%; border-radius:99px;
                 background:linear-gradient(90deg,{GOLD},{EMERALD}); }}

  /* ---------- comparateur d'équipes ---------- */
  .ps-vs {{ display:grid; grid-template-columns:1fr auto 1fr; gap:.5rem;
            align-items:center; padding:.34rem 0;
            border-bottom:1px dashed rgba(255,255,255,.05); }}
  .ps-vs:last-child {{ border-bottom:none; }}
  .ps-vs .n {{ font-variant-numeric:tabular-nums; font-weight:650; font-size:.92rem; }}
  .ps-vs .l {{ text-align:right; }}
  .ps-vs .r {{ text-align:left; }}
  .ps-vs .k {{ color:{MUTED}; font-size:.7rem; text-transform:uppercase;
               letter-spacing:.08em; white-space:nowrap; padding:0 .5rem; }}
  .ps-vs .win {{ color:{GOLD}; }}
  .ps-heads {{ display:grid; grid-template-columns:1fr auto 1fr; gap:.5rem;
               padding-bottom:.4rem; margin-bottom:.2rem;
               border-bottom:1px solid {LINE}; }}
  .ps-heads .l {{ text-align:right; }}
  .ps-heads .r {{ text-align:left; }}
  .ps-heads b {{ font-size:.9rem; }}

  /* ---------- score exact ---------- */
  .ps-score {{ display:flex; gap:.5rem; flex-wrap:wrap; }}
  .ps-score .cell {{ flex:1; min-width:74px; text-align:center; padding:.55rem .3rem;
                     border:1px solid {LINE}; border-radius:12px; background:#111823; }}
  .ps-score .cell.top {{ border-color:rgba(217,180,91,.45); background:rgba(217,180,91,.07); }}
  .ps-score .cell b {{ display:block; font-size:1.1rem; font-variant-numeric:tabular-nums; }}
  .ps-score .cell s {{ display:block; text-decoration:none; color:{MUTED}; font-size:.74rem; }}

  /* ---------- bandeau jeu responsable ---------- */
  .ps-banner {{ position:sticky; bottom:0; margin-top:1.6rem; text-align:center;
                font-size:.76rem; color:{MUTED}; background:rgba(11,15,20,.92);
                border-top:1px solid {LINE}; padding:.5rem; backdrop-filter:blur(6px); }}

  /* ---------- contrôles Streamlit ---------- */
  div[data-testid="stSelectbox"] label, div[data-testid="stRadio"] label {{
      color:{MUTED} !important; font-size:.75rem !important; letter-spacing:.1em;
      text-transform:uppercase; font-weight:600;
  }}
  .stButton > button {{
      width:100%; border-radius:12px; font-weight:700; letter-spacing:.02em;
      border:1px solid rgba(217,180,91,.5); padding:.62rem 1rem;
      background:linear-gradient(180deg, rgba(217,180,91,.22), rgba(217,180,91,.10));
      color:{GOLD}; transition:all .15s ease;
  }}
  .stButton > button:hover {{ border-color:{GOLD}; color:#fff;
      background:linear-gradient(180deg, rgba(217,180,91,.34), rgba(217,180,91,.16)); }}
  .stButton > button:disabled {{ color:{MUTED}; border-color:{LINE}; background:#131A23; }}
  div[data-testid="stExpander"] {{ border:1px solid {LINE}; border-radius:14px;
                                   background:{CARD}; }}
  hr {{ border-color:{LINE}; }}

  @media (max-width: 640px) {{
      .ps-big {{ font-size:1.7rem; }}
      .block-container {{ padding-left:.8rem; padding-right:.8rem; }}
      .ps-vs .k {{ font-size:.62rem; padding:0 .25rem; }}
  }}
</style>
"""
st.markdown(STYLE, unsafe_allow_html=True)

PLOT_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color=TEXT, family="Inter, system-ui, sans-serif", size=12),
    margin=dict(l=8, r=8, t=8, b=8),
)


# ==========================================================================
# Ressources partagées
# ==========================================================================
def _code_version() -> str:
    """Empreinte du code actuellement chargé.

    Sert de clé de cache aux objets partagés. Sans elle, une instance
    construite par la version précédente peut survivre à un redéploiement :
    `st.cache_resource` conserve ses objets tant que le processus vit, et
    l'application plante alors sur une méthode qui n'existait pas encore
    — un `AttributeError` incompréhensible, puisque le code source, lui,
    est bien à jour.

    L'empreinte se calcule une seule fois, à l'import.
    """
    marques = []
    for module in (cfg, data_sources_module, engine_module):
        chemin = getattr(module, "__file__", None)
        if not chemin:
            continue
        try:
            infos = Path(chemin).stat()
        except OSError:
            continue
        marques.append(f"{Path(chemin).name}:{infos.st_mtime_ns}:{infos.st_size}")
    return hashlib.sha1("|".join(marques).encode()).hexdigest()[:12] if marques else "inconnu"


CODE_VERSION = _code_version()


@st.cache_resource(show_spinner=False)
def _cache_marker() -> dict[str, str]:
    """Témoin de la version du code qui a rempli les caches."""
    return {"version": CODE_VERSION}


def drop_stale_caches() -> None:
    """Vide les caches quand le code a changé depuis qu'ils ont été remplis.

    `st.cache_resource` conserve ses objets tant que le processus vit — y
    compris au travers d'une mise à jour du code. Une instance construite par
    la version précédente survit alors, et l'application échoue sur une
    méthode ajoutée depuis, avec un `AttributeError` d'autant plus
    déroutant que le fichier source, lui, est bien à jour.

    Ce contrôle ne coûte rien : il ne vide les caches que lorsque
    l'empreinte du code a réellement changé.
    """
    if _cache_marker().get("version") == CODE_VERSION:
        return
    st.cache_data.clear()
    st.cache_resource.clear()
    _cache_marker()  # recréé avec la version courante


@st.cache_resource(show_spinner=False)
def get_hub(version: str = CODE_VERSION) -> DataHub:
    return DataHub()


@st.cache_resource(show_spinner=False)
def get_agent(version: str = CODE_VERSION) -> AnalysisAgent:
    """L'agent d'analyse. Un seul par session : il porte le journal local."""
    return AnalysisAgent(get_hub())


@st.cache_resource(show_spinner=False)
def get_advisor(version: str = CODE_VERSION) -> TuningAdvisor:
    return TuningAdvisor()


@st.cache_data(ttl=cfg.TTL.roster, show_spinner=False)
def load_roster(sport: str, comp_key: str) -> tuple[list[str], float | None, float]:
    """Effectif complet de la compétition : noms, couverture, fiabilité.

    Mis en cache : les effectifs changent une fois par saison, il est inutile
    de réinterroger les sources à chaque interaction.
    """
    comp = cfg.competition(sport, comp_key)
    if comp is None:
        return [], None, 0.0
    result = get_hub().roster(comp)
    return result.names, result.coverage, result.reliability


@st.cache_data(ttl=cfg.TTL.events, show_spinner=False)
def load_fixtures(sport: str, comp_key: str) -> list[tuple[str, str, str]]:
    """Affiches réellement programmées : (domicile, extérieur, libellé).

    Renvoie des tuples plutôt que des objets : `st.cache_data` sérialise son
    résultat, et des types simples s'y prêtent sans surprise.

    L'appel ne consomme aucun crédit — l'endpoint calendrier est gratuit.
    """
    comp = cfg.competition(sport, comp_key)
    if comp is None:
        return []
    try:
        return [(f.home, f.away, f.label) for f in get_hub().fixtures(comp)]
    except Exception:
        # Le calendrier est un confort, pas une dépendance : sans lui
        # l'application retombe sur le choix libre des deux équipes. Aucune
        # raison de faire tomber toute la page.
        return []


# ==========================================================================
# Petits composants HTML
# ==========================================================================
def esc(text) -> str:
    return html.escape(str(text))


def pct(x: float | None, digits: int = 0) -> str:
    return "—" if x is None else f"{100 * x:.{digits}f} %"


def num(x: float | None, digits: int = 1, suffix: str = "") -> str:
    return "—" if x is None else f"{x:.{digits}f}".replace(".", ",") + suffix


def badge(text: str, kind: str = "") -> str:
    return f'<span class="ps-badge {kind}">{esc(text)}</span>'


def card(title: str, body: str) -> str:
    return f'<div class="ps-card"><div class="ps-card-title">{esc(title)}</div>{body}</div>'


def prob_row(label: str, prob: float, extra: str = "") -> str:
    width = max(0.0, min(1.0, prob)) * 100
    return (
        f'<div class="ps-row"><div style="flex:1">'
        f'<div style="display:flex;justify-content:space-between">'
        f'<span class="lbl">{esc(label)}</span>'
        f'<span class="val">{pct(prob)}{extra}</span></div>'
        f'<div class="ps-bar"><i style="width:{width:.1f}%"></i></div>'
        f"</div></div>"
    )


def de(name: str) -> str:
    """« de » avec élision : de Chelsea, mais d'Arsenal."""
    return f"d'{name}" if name[:1].upper() in "AEIOUYÀÂÉÈÊÎÔÛ" else f"de {name}"


def opponent_options(teams: list[str], chosen: str | None) -> list[str]:
    """Liste adverse : l'équipe déjà sélectionnée en est retirée.

    Garantit qu'un même club ne peut pas être choisi des deux côtés, sans
    aucune action de l'utilisateur.
    """
    return [t for t in teams if t and t != chosen]


def vs_row(label: str, left: str, right: str, left_better: bool | None) -> str:
    lc = " win" if left_better is True else ""
    rc = " win" if left_better is False else ""
    return (
        f'<div class="ps-vs"><div class="n l{lc}">{esc(left)}</div>'
        f'<div class="k">{esc(label)}</div>'
        f'<div class="n r{rc}">{esc(right)}</div></div>'
    )


# ==========================================================================
# En-tête + quotas
# ==========================================================================
def render_header() -> None:
    hub = get_hub()
    chips = []
    for status in hub.quota_status():
        if status.exhausted:
            kind, txt = "warn", f"{status.label} : quota épuisé"
        elif status.warning:
            kind, txt = "warn", f"{status.label} : {status.remaining} restants"
        else:
            kind, txt = "", f"{status.label} : {status.remaining}"
        chips.append(badge(txt, kind))
    if cfg.PREMIUM_MODE:
        chips.append(badge("Mode premium", "gold"))

    st.markdown(
        f"""
        <div class="ps-header">
          <div class="ps-brand">
            <div class="ps-logo">Prono<span>Stat</span></div>
            <div class="ps-tag">Compétitions majeures · Analyse statistique</div>
          </div>
          <div class="ps-chips">{''.join(chips)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    for status in hub.quota_status():
        if status.exhausted:
            st.warning(
                f"{status.label} — quota épuisé. L'app bascule sur les données en cache "
                f"(réessayez {'demain' if status.period == 'day' else 'le mois prochain'} "
                "ou passez à une offre payante).",
                icon="⚠️",
            )
        elif status.warning:
            st.info(
                f"{status.label} — limite bientôt atteinte ({status.used}/{status.limit}).",
                icon="ℹ️",
            )


# ==========================================================================
# Sélection : sport → compétition → équipes
# ==========================================================================
def render_controls() -> tuple[Competition | None, str | None, str | None, bool]:
    sport_keys = list(cfg.SPORTS)
    labels = {k: f"{v['icon']}  {v['label']}" for k, v in cfg.SPORTS.items()}

    if hasattr(st, "segmented_control"):
        sport = st.segmented_control(
            "Sport",
            options=sport_keys,
            format_func=lambda k: labels[k],
            default=sport_keys[0],
            key="sport",
        ) or sport_keys[0]
    else:  # compatibilité Streamlit < 1.40
        sport = st.radio(
            "Sport",
            options=sport_keys,
            format_func=lambda k: labels[k],
            horizontal=True,
            key="sport",
        )

    comps = cfg.competitions(sport)
    if not comps:
        st.error("Aucune compétition activée pour ce sport.", icon="🚫")
        return None, None, None, False

    col_comp, col_info = st.columns([2, 3], vertical_alignment="bottom")
    with col_comp:
        comp = st.selectbox(
            "Compétition",
            options=comps,
            format_func=lambda c: c.label,
            index=0,
            key=f"comp_{sport}",
        )
    with col_info:
        tag = badge("Coupe", "gold") if comp.is_cup else badge("Championnat")
        tier = badge("Compétition majeure", "em") if comp.tier == 1 else badge("Secondaire")
        st.markdown(
            f'<div style="padding-bottom:.55rem">{tag}{tier}</div>',
            unsafe_allow_html=True,
        )

    with st.spinner(f"Chargement des équipes · {comp.label}…"):
        teams, coverage, _reliability = load_roster(sport, comp.key)

    if not teams:
        # En ligne il n'existe aucun fichier `.env` : les clés se saisissent
        # dans Settings → Secrets. Indiquer le mauvais endroit enverrait
        # l'utilisateur chercher un fichier qui n'existe pas sur le serveur.
        where = (
            "dans **Settings → Secrets** de votre application"
            if cfg.IS_PRODUCTION
            else "dans `.env`"
        )
        hint = {
            "tennis": "Le tennis n'a pas de source gratuite sans clé : renseignez "
            f"`ODDS_API_KEY` {where} pour charger les joueurs des tournois en cours.",
            "basket": f"Renseignez `BALLDONTLIE_API_KEY` (NBA) ou `ODDS_API_KEY` {where}.",
        }.get(
            sport,
            f"Renseignez `ODDS_API_KEY`, `FOOTBALL_DATA_API_KEY` ou `RAPIDAPI_KEY` {where}.",
        )
        st.error(f"Aucun participant chargé pour {comp.label}. {hint}", icon="🚫")
        st.caption(
            "Les tournois de tennis n'existent dans les API que pendant leur "
            "déroulement ; hors période, la liste est logiquement vide."
            if sport == "tennis"
            else "Vérifiez aussi votre connexion internet."
        )
        return comp, None, None, False

    # Les affiches réellement au calendrier. Une compétition de vingt équipes
    # offre 380 appariements, mais une dizaine seulement se jouent : choisir
    # librement mène presque toujours à un match qui n'existe pas, donc sans
    # la moindre cote. On propose donc le calendrier en premier.
    with st.spinner("Recherche des matchs programmés…"):
        fixtures = load_fixtures(sport, comp.key)

    mode_key = f"mode_{comp.key}"
    if fixtures:
        mode = st.radio(
            "Choix du match",
            options=("calendrier", "libre"),
            format_func=lambda m: (
                f"📅  Matchs programmés ({len(fixtures)})" if m == "calendrier"
                else "🔀  Deux adversaires au choix"
            ),
            horizontal=True,
            key=mode_key,
            label_visibility="collapsed",
        )
    else:
        mode = "libre"

    if mode == "calendrier":
        col_match, col_go = st.columns([8, 2], vertical_alignment="bottom")
        with col_match:
            choix = st.selectbox(
                "Match à l'affiche",
                options=range(len(fixtures)),
                format_func=lambda i: fixtures[i][2],
                key=f"fixture_{comp.key}",
            )
        home, away = fixtures[choix][0], fixtures[choix][1]
        with col_go:
            launch = st.button(
                "Démarrer la simulation",
                type="primary",
                use_container_width=True,
            )
        st.caption(
            "Rencontres réellement au calendrier. Choisir ici plutôt que de composer "
            "librement deux équipes évite les affiches qui n'existent pas — les seules "
            "à pouvoir porter des cotes sont celles programmées."
        )
        return comp, home, away, launch

    col_a, col_b, col_c = st.columns([4, 4, 2], vertical_alignment="bottom")
    with col_a:
        home = st.selectbox(
            "Équipe / joueur 1 (domicile)", teams, index=0, key=f"home_{comp.key}"
        )
    with col_b:
        # L'équipe déjà choisie disparaît de l'autre liste : impossible de
        # sélectionner deux fois la même. Si le choix précédent devient
        # invalide, on le réinitialise avant d'afficher le menu.
        away_key = f"away_{comp.key}"
        away_options = opponent_options(teams, home)
        if st.session_state.get(away_key) not in away_options:
            st.session_state.pop(away_key, None)
        away = st.selectbox(
            "Équipe / joueur 2 (extérieur)", away_options, key=away_key
        ) if away_options else None
    with col_c:
        launch = st.button(
            "Démarrer la simulation",
            type="primary",
            disabled=not away,
            use_container_width=True,
        )

    # Prévenir AVANT de lancer, pas après : sans cet avertissement, l'analyse
    # tourne une minute pour finir sur un « cotes indisponibles » que rien ne
    # laissait prévoir.
    if fixtures and home and away:
        programme = any(
            {normalize_name(h), normalize_name(a)} == {normalize_name(home), normalize_name(away)}
            for h, a, _ in fixtures
        )
        if not programme:
            st.warning(
                f"**{home} — {away} n'est pas au calendrier.** L'analyse "
                "fonctionnera, mais sans cotes réelles : elle reposera sur les "
                "seules statistiques, avec une confiance nettement plus basse.",
                icon="📅",
            )
    note = f"{len(teams)} équipes disponibles · {comp.label}"
    if coverage is not None and coverage < 0.98:
        note += (
            f" — liste partielle ({coverage:.0%} de l'effectif). "
            "Ajoutez une clé dans la configuration pour la compléter."
        )
    st.caption(note)
    return comp, home, away, launch


# ==========================================================================
# Blocs de résultats
# ==========================================================================
def render_main_pick(result: AnalysisResult) -> None:
    """La décision de l'agent, mise en avant avant tout le reste."""
    decision = result.decision
    pred = result.prediction

    if decision.abstained:
        st.markdown(
            f'<div class="ps-card" style="border-left:3px solid {RED}">'
            f'<div class="ps-card-title">Notre pronostic</div>'
            f'<div class="ps-mid">{esc(decision.recommendation)}</div>'
            f'<div class="ps-sub">{esc(decision.rationale)}</div></div>',
            unsafe_allow_html=True,
        )
        return

    tone = (
        EMERALD if decision.probability >= 0.60
        else (GOLD if decision.probability >= 0.45 else RED)
    )
    chips = [badge(decision.market, "gold"), badge(f"Signal {decision.strength.lower()}")]
    if decision.is_value:
        chips.append(
            badge(f"Sous-évalué par les bookmakers (+{100*(decision.edge or 0):.0f} pts)", "em")
        )
    if decision.odds:
        chips.append(badge(f"Cote {decision.odds:.2f}"))

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        st.markdown(
            f'<div class="ps-card" style="border-left:3px solid {tone}">'
            f'<div class="ps-card-title">Notre pronostic</div>'
            f'<div class="ps-big" style="color:{tone}">{esc(decision.recommendation)}</div>'
            f'<div class="ps-sub">{esc(decision.rationale)}</div>'
            f'<div style="margin-top:.55rem">{"".join(chips)}</div></div>',
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            card(
                "Chances de réussite",
                f'<div class="ps-big">{pct(decision.probability, 0)}</div>'
                f'<div class="ps-bar" style="margin-top:.5rem">'
                f'<i style="width:{decision.probability * 100:.0f}%"></i></div>'
                f'<div class="ps-sub">{pred.n_sims:,} scénarios simulés</div>'.replace(",", " "),
            ),
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            card(
                "Notre confiance",
                f'<div class="ps-big">{num(decision.confidence, 1)}'
                f'<span style="font-size:1rem;color:{MUTED}">/10</span></div>'
                f'<div class="ps-bar" style="margin-top:.5rem">'
                f'<i style="width:{decision.confidence * 10:.0f}%"></i></div>'
                f'<div class="ps-sub">{esc(decision.assessment.label)}</div>',
            ),
            unsafe_allow_html=True,
        )


def render_reasoning(result: AnalysisResult) -> None:
    """Ce qui a fait pencher la décision, et ce qui pourrait la faire tomber."""
    decision = result.decision
    c1, c2 = st.columns([1, 1])

    with c1:
        rows = []
        for factor in decision.key_factors:
            side = result.prediction.home if factor.value > 0 else result.prediction.away
            colour = EMERALD if factor.value > 0 else GOLD
            width = min(100, abs(factor.value) * 100)
            rows.append(
                f'<div class="ps-row"><div style="flex:1">'
                f'<div style="display:flex;justify-content:space-between">'
                f'<span class="lbl">{esc(factor.label)}</span>'
                f'<span class="val" style="color:{colour};font-size:.82rem">'
                f"{esc(side)}</span></div>"
                f'<div class="ps-bar"><i style="width:{width:.0f}%;'
                f'background:{colour}"></i></div>'
                f'<div class="ps-sub" style="font-size:.74rem;margin-top:.15rem">'
                f"{esc(factor.detail)}</div></div></div>"
            )
        body = "".join(rows) or (
            f'<div class="ps-sub">{badge("Aucun critère déterminant", "warn")}</div>'
        )
        st.markdown(card("Ce qui a fait pencher la balance", body), unsafe_allow_html=True)

    with c2:
        rows = [
            f'<div class="ps-row"><span class="lbl">⚠ {esc(risk.text)}</span>'
            f'<span class="val">{pct(risk.probability)}</span></div>'
            for risk in decision.risks
        ]
        if decision.contradictions:
            rows.append(
                f'<div style="margin-top:.6rem">'
                + "".join(badge(c.text, "warn") for c in decision.contradictions[:2])
                + "</div>"
            )
        body = "".join(rows) or (
            f'<div class="ps-sub">{badge("Aucun risque majeur identifié", "em")}</div>'
        )
        st.markdown(card("Ce qui pourrait faire basculer le match", body),
                    unsafe_allow_html=True)


def render_headline(pred: Prediction) -> None:
    """Deuxième niveau de lecture : issue attendue et ce qui a servi au calcul.

    Volontairement sans redite du bloc « Notre pronostic » : ni le favori ni la
    confiance n'y sont répétés.
    """
    c1, c2, c3 = st.columns([1, 1, 1.35])

    with c1:
        if pred.top_scores and pred.sport != "basket":
            label = "Score le plus probable" if pred.sport != "tennis" else "Score en sets"
            top = pred.top_scores[0]
            body = (
                f'<div class="ps-big">{esc(top[0])}</div>'
                f'<div class="ps-sub">{pct(top[1], 1)} des simulations</div>'
            )
        else:
            label = "Score attendu"
            body = (
                f'<div class="ps-big">{pred.expected.get("points_home", 0):.0f}'
                f' – {pred.expected.get("points_away", 0):.0f}</div>'
                f'<div class="ps-sub">écart moyen '
                f'{pred.expected.get("margin", 0):+.1f} pts</div>'
            )
        st.markdown(card(label, body), unsafe_allow_html=True)

    with c2:
        favourite_gap = abs(
            pred.outcome_probs.get("home", 0) - pred.outcome_probs.get("away", 0)
        )
        st.markdown(
            card(
                "Écart entre les deux",
                f'<div class="ps-big">{pct(favourite_gap, 0)}</div>'
                f'<div class="ps-sub">en faveur {esc(de(pred.favorite))}</div>'
                f'<div class="ps-bar" style="margin-top:.5rem">'
                f'<i style="width:{min(100, favourite_gap * 200):.0f}%"></i></div>',
            ),
            unsafe_allow_html=True,
        )

    with c3:
        badges = "".join(
            badge(
                b,
                "em" if ("Opportunité" in b or "avancées" in b or "Classement" in b)
                else ("warn" if "indisponible" in b or "incomplet" in b else ""),
            )
            for b in pred.badges
        )
        comp_line = (
            f'<div class="ps-sub">{esc(pred.competition.label)}</div>'
            if pred.competition
            else ""
        )
        st.markdown(
            card("Ce qui a servi au calcul", comp_line
                 + f'<div style="margin-top:.4rem">{badges}</div>'),
            unsafe_allow_html=True,
        )


def render_probabilities(pred: Prediction) -> None:
    keys = ["home", "draw", "away"] if "draw" in pred.outcome_probs else ["home", "away"]
    names = {"home": pred.home, "draw": "Match nul", "away": pred.away}
    model_vals = [pred.outcome_probs.get(k, 0.0) for k in keys]
    labels = [names[k] for k in keys]

    fig = go.Figure()
    fig.add_bar(
        y=labels, x=model_vals, orientation="h", name="Modèle",
        marker=dict(color=GOLD, line=dict(width=0)),
        text=[f"{v*100:.1f} %" for v in model_vals], textposition="auto",
        insidetextfont=dict(color="#12181F", size=12),
    )
    if pred.market_probs:
        market_vals = [pred.market_probs.get(k, 0.0) for k in keys]
        fig.add_bar(
            y=labels, x=market_vals, orientation="h", name="Bookmakers",
            marker=dict(color=BLUE, line=dict(width=0)),
            text=[f"{v*100:.1f} %" for v in market_vals], textposition="auto",
            insidetextfont=dict(color="#12181F", size=12),
        )
    fig.update_layout(
        **PLOT_LAYOUT, barmode="group", height=90 + 58 * len(keys),
        xaxis=dict(range=[0, 1], showgrid=True, gridcolor="rgba(255,255,255,.06)",
                   tickformat=".0%"),
        yaxis=dict(showgrid=False),
        legend=dict(orientation="h", y=1.18, x=0, bgcolor="rgba(0,0,0,0)"),
        showlegend=bool(pred.market_probs),
    )

    col1, col2 = st.columns([1.5, 1])
    with col1:
        st.markdown('<div class="ps-card-title">Probabilités de résultat</div>',
                    unsafe_allow_html=True)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        if not pred.market_probs:
            st.markdown(badge("Cotes indisponibles — modèle seul", "warn"),
                        unsafe_allow_html=True)

    with col2:
        if pred.top_scores and pred.sport != "basket":
            cells = "".join(
                f'<div class="cell{" top" if i == 0 else ""}"><b>{esc(s)}</b>'
                f"<s>{pct(p, 1)}</s></div>"
                for i, (s, p) in enumerate(pred.top_scores[:5])
            )
            body = f'<div class="ps-score">{cells}</div>'
            title = "Scénarios en sets" if pred.sport == "tennis" else "Top 5 des scores exacts"
        else:
            body = (
                f'<div class="ps-mid">{pred.expected.get("points_total", 0):.0f} points</div>'
                f'<div class="ps-sub">total attendu · écart type '
                f'{pred.expected.get("margin_sd", 0):.1f}</div>'
            )
            title = "Volume de jeu"
        st.markdown(card(title, body), unsafe_allow_html=True)


def render_profiles(pred: Prediction) -> None:
    """Comparateur statistique compact — uniquement les données réellement reçues."""
    ph, pa = pred.profile_home, pred.profile_away
    if not ph or not pa or (ph.matches == 0 and pa.matches == 0):
        return

    is_pct = {"clean_sheet_rate", "btts_rate", "over_rate"}
    rows_spec = [
        ("rank", "Classement", 0, True),          # plus petit = mieux
        ("points_per_game", "Pts / match", 2, False),
        ("scored_avg", "Marqués", 2, False),
        ("conceded_avg", "Encaissés", 2, True),
        ("scored_home", "Marqués dom.", 2, False),
        ("scored_away", "Marqués ext.", 2, False),
        ("xg_for", "xG pour", 2, False),
        ("xg_against", "xG contre", 2, True),
        ("clean_sheet_rate", "Matchs sans encaisser", 0, False),
        ("btts_rate", "Les 2 marquent", 0, False),
        ("over_rate", "Matchs prolifiques", 0, False),
        ("possession", "Possession", 0, False),
        ("shots", "Tirs", 1, False),
        ("shots_on_target", "Tirs cadrés", 1, False),
        ("corners_for", "Corners pour", 1, False),
        ("cards", "Cartons jaunes", 1, True),
        ("rest_days", "Jours de repos", 1, False),
    ]

    rows = []
    for key, label, digits, lower_better in rows_spec:
        left, right = getattr(ph, key, None), getattr(pa, key, None)
        if left is None and right is None:
            continue
        if key in is_pct:
            l_txt, r_txt = pct(left), pct(right)
        elif key == "possession":
            l_txt, r_txt = num(left, 0, " %"), num(right, 0, " %")
        elif key == "rank":
            l_txt = "—" if left is None else f"#{int(left)}"
            r_txt = "—" if right is None else f"#{int(right)}"
        else:
            l_txt, r_txt = num(left, digits), num(right, digits)
        better = None
        if left is not None and right is not None and left != right:
            better = (left < right) if lower_better else (left > right)
        rows.append(vs_row(label, l_txt, r_txt, better))

    if not rows:
        return

    header = (
        f'<div class="ps-heads"><div class="l"><b>{esc(pred.home)}</b></div>'
        f'<div class="k">&nbsp;</div>'
        f'<div class="r"><b>{esc(pred.away)}</b></div></div>'
    )
    form_line = (
        f'<div class="ps-vs"><div class="n l">{esc(ph.form_string or "—")}</div>'
        f'<div class="k">Forme (5)</div>'
        f'<div class="n r">{esc(pa.form_string or "—")}</div></div>'
    )
    sample = badge(f"{ph.matches} / {pa.matches} matchs analysés")
    st.markdown(
        card("Profils statistiques", header + form_line + "".join(rows)
             + f'<div style="margin-top:.6rem">{sample}</div>'),
        unsafe_allow_html=True,
    )


def render_h2h(pred: Prediction) -> None:
    summary = pred.h2h_summary
    if not summary:
        st.markdown(
            card(
                "Confrontations directes",
                f'<div class="ps-sub">{badge("Aucune donnée", "warn")} '
                "Pas d'historique commun trouvé.</div>",
            ),
            unsafe_allow_html=True,
        )
        return
    body = (
        f'<div class="ps-mid">{summary["home_wins"]} – {summary["draws"]} – '
        f'{summary["away_wins"]}</div>'
        f'<div class="ps-sub">sur {summary["n"]} confrontation(s) · '
        f'{num(summary["avg_goals"], 1)} buts en moyenne</div>'
    )
    body += "".join(
        f'<div class="ps-row"><span class="lbl">{esc(d)}</span>'
        f'<span class="val">{esc(s)}</span></div>'
        for d, s in summary.get("last", [])
    )
    st.markdown(card("Confrontations directes", body), unsafe_allow_html=True)


def render_score_heatmap(pred: Prediction) -> None:
    if pred.score_matrix is None:
        return
    size = min(6, pred.score_matrix.shape[0])
    mat = pred.score_matrix[:size, :size] * 100
    fig = go.Figure(
        go.Heatmap(
            z=mat, x=[str(i) for i in range(size)], y=[str(i) for i in range(size)],
            colorscale=[[0, "#111823"], [0.4, "#1F5F52"], [1, GOLD]], showscale=False,
            hovertemplate="%{y} – %{x} : %{z:.1f} %<extra></extra>",
        )
    )
    fig.update_layout(
        **PLOT_LAYOUT, height=280,
        xaxis=dict(title=dict(text=pred.away, font=dict(size=11, color=MUTED)), side="top"),
        yaxis=dict(title=dict(text=pred.home, font=dict(size=11, color=MUTED)),
                   autorange="reversed"),
    )
    st.markdown('<div class="ps-card-title">Distribution des scores</div>',
                unsafe_allow_html=True)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def render_markets(pred: Prediction) -> None:
    cols = st.columns(3)

    totals = [l for l in pred.lines if l.key.startswith("total_over_")]
    totals.sort(key=lambda l: abs(l.prob - 0.5))
    with cols[0]:
        unit = {"tennis": "jeux", "basket": "points"}.get(pred.sport, "buts")
        body = "".join(prob_row(l.label, l.prob) for l in totals[:3]) or (
            f'<div class="ps-sub">{badge("Données indisponibles", "warn")}</div>'
        )
        st.markdown(card(f"Nombre de {unit}", body), unsafe_allow_html=True)

    with cols[1]:
        if pred.sport in {"football", "hockey"}:
            parts = []
            for key in ("btts_yes", "dc_1x", "dc_x2"):
                line = pred.line(key)
                if line:
                    parts.append(prob_row(
                        "Les deux marquent" if key == "btts_yes" else line.label, line.prob
                    ))
            st.markdown(card("Marchés dérivés", "".join(parts)), unsafe_allow_html=True)
        else:
            extra = [l for l in pred.lines
                     if l.key.startswith("spread_") or l.key.startswith("sets_")]
            body = "".join(prob_row(l.label, l.prob) for l in extra[:4]) or (
                f'<div class="ps-sub">{badge("Données indisponibles", "warn")}</div>'
            )
            st.markdown(
                card("Handicap" if pred.sport == "basket" else "Scores en sets", body),
                unsafe_allow_html=True,
            )

    with cols[2]:
        if pred.sport == "football":
            corners = [l for l in pred.lines if l.key.startswith("corners_over_")]
            if corners:
                head = (
                    f'<div class="ps-mid">{num(pred.expected.get("corners_total"))} '
                    "corners attendus</div>"
                    f'<div class="ps-sub">'
                    f'{badge("Échantillon limité", "warn") if corners[0].note else badge("Mesuré sur les derniers matchs", "em")}'
                    "</div>"
                )
                body = head + "".join(prob_row(l.label, l.prob) for l in corners)
            else:
                body = (
                    f'<div class="ps-sub">{badge("Données insuffisantes", "warn")}</div>'
                    '<div class="ps-sub">Aucune source ne publie le nombre de '
                    "corners pour ces équipes.</div>"
                )
            st.markdown(card("Corners", body), unsafe_allow_html=True)
        elif pred.sport == "hockey":
            pl = [l for l in pred.lines if l.key.startswith("puckline_")]
            ml = [l for l in pred.lines if l.key.startswith("ml_")]
            body = "".join(prob_row(l.label, l.prob) for l in ml + pl[:2])
            body += f'<div class="ps-sub">Prolongation : {pct(pred.expected.get("p_overtime"))}</div>'
            st.markdown(card("Puck line & prolongation", body), unsafe_allow_html=True)
        elif pred.sport == "basket":
            body = (
                f'<div class="ps-big">{pred.expected.get("margin", 0):+.1f}</div>'
                f'<div class="ps-sub">écart attendu (domicile) · σ = '
                f'{pred.expected.get("margin_sd", 0):.1f}</div>'
            )
            st.markdown(card("Écart attendu", body), unsafe_allow_html=True)
        else:
            body = (
                f'<div class="ps-big">{pred.expected.get("games_total", 0):.1f}</div>'
                f'<div class="ps-sub">jeux attendus · tenue de service '
                f'{pct(pred.expected.get("hold_home"))} / '
                f'{pct(pred.expected.get("hold_away"))}</div>'
            )
            st.markdown(card("Total de jeux", body), unsafe_allow_html=True)


@st.cache_data(ttl=cfg.TTL.odds, show_spinner=False)
def load_scorers(sport: str, comp_key: str, home: str, away: str) -> list[tuple[str, float, float]]:
    """Buteurs cotés par le marché : (joueur, probabilité, cote)."""
    comp = cfg.competition(sport, comp_key)
    if comp is None:
        return []
    try:
        board = get_hub().goal_scorers(comp, home, away)
    except Exception:
        return []      # carte « données indisponibles », jamais une page en erreur
    return [(s.player, s.probability, s.price) for s in board.scorers] if board else []


def render_scorers(pred: Prediction) -> None:
    """Buteurs probables — uniquement au football, uniquement si le marché les cote.

    Rien n'est déduit de la forme collective : sans cotes buteur, la carte
    affiche « données indisponibles » plutôt qu'un classement fabriqué.
    """
    if pred.sport != "football" or pred.competition is None:
        return

    with st.spinner("Recherche des cotes buteur…"):
        scorers = load_scorers(pred.sport, pred.competition.key, pred.home, pred.away)

    if not scorers:
        body = (
            f'<div class="ps-sub">{badge("Données indisponibles", "warn")}</div>'
            '<div class="ps-sub" style="margin-top:.5rem">Les bookmakers ne publient '
            "les cotes buteur qu'à l'approche du coup d'envoi, généralement moins de "
            "trois jours avant.</div>"
        )
        st.markdown(card("Buteurs probables", body), unsafe_allow_html=True)
        return

    # La cote passe par `extra` : `prob_row` échappe son libellé, un balisage
    # glissé dedans s'afficherait tel quel.
    lignes = "".join(
        prob_row(joueur, proba, extra=f' <span class="ps-tag">{cote:.2f}</span>')
        for joueur, proba, cote in scorers[:8]
    )
    note = (
        '<div class="ps-sub" style="margin-top:.6rem">'
        f'{badge("Probabilité implicite du marché", "gold")} '
        "Marge du bookmaker incluse : ces valeurs sont légèrement surestimées. "
        "Leur somme dépasse 100 %, plusieurs joueurs marquant souvent dans le même match."
        "</div>"
    )
    st.markdown(card("Buteurs probables", lignes + note), unsafe_allow_html=True)


def render_market_comparison(pred: Prediction, market=None) -> None:
    compared = [l for l in pred.lines if l.market_prob is not None]
    title = "Notre estimation face aux bookmakers"
    if not compared:
        # On explique la cause exacte plutôt qu'un « indisponible » muet.
        reason = getattr(market, "unavailable_reason", None) or "Aucune cote publiée"
        hint = getattr(market, "unavailable_hint", None)
        body = (
            f'<div class="ps-sub">{badge("Cotes du match indisponibles", "warn")}</div>'
            f'<div class="ps-sub" style="margin-top:.4rem">{esc(reason)}.</div>'
        )
        reference = getattr(market, "reference", None)
        if reference:
            rows = "".join(
                prob_row(label, reference.get(key, 0.0))
                for key, label in (("home", pred.home), ("draw", "Match nul"),
                                   ("away", pred.away))
                if key in reference
            )
            body += (
                f'<div style="margin-top:.7rem">{badge("Repère de saison", "gold")}</div>'
                '<div class="ps-sub">Ce que les bookmakers accordaient à ces deux '
                "équipes tout au long de la saison — pas une cote de ce match.</div>"
                + rows
            )
        elif hint:
            body += f'<div class="ps-sub" style="margin-top:.3rem">{esc(hint)}</div>'
        st.markdown(card(title, body), unsafe_allow_html=True)
        return

    compared.sort(key=lambda l: -(l.edge or -9))
    rows = []
    for line in compared[:6]:
        tag = badge(f"+{100*(line.edge or 0):.1f} pts", "em") if line.is_value else ""
        rows.append(
            f'<div class="ps-row"><span class="lbl">{esc(line.label)}</span>'
            f'<span class="val">{pct(line.prob, 1)} '
            f'<span style="color:{MUTED};font-weight:400">vs {pct(line.market_prob, 1)}</span> '
            f"{tag}</span></div>"
        )
    body = "".join(rows)
    if pred.value_bets:
        best = pred.value_bets[0]
        body += (
            f'<div style="margin-top:.7rem">{badge("Opportunité repérée", "em")} '
            f'<span class="ps-sub">{esc(best.label)} · cote {best.odds:.2f} · '
            f"gain attendu {100 * (best.expected_value or 0):+.0f} %</span></div>"
        )
    else:
        body += f'<div style="margin-top:.7rem">{badge("Cotes conformes à notre estimation")}</div>'
    st.markdown(card(title, body), unsafe_allow_html=True)


def render_context(pred: Prediction) -> None:
    """Contexte : bookmakers, dérive des cotes, météo, actualité."""
    bits = []
    if pred.bookmaker_count:
        bits.append(badge(f"{pred.bookmaker_count} bookmakers", "em"))
    if pred.odds_movement:
        biggest = max(pred.odds_movement.items(), key=lambda kv: abs(kv[1]))
        arrow = "▲" if biggest[1] > 0 else "▼"
        hours = pred.odds_movement_hours or 0
        kind = "warn" if abs(biggest[1]) >= 0.08 else ""
        bits.append(
            badge(f"{arrow} {esc(biggest[0])} {biggest[1]*100:+.1f} % / {hours:.0f} h", kind)
        )
    if pred.weather is not None:
        kind = "warn" if getattr(pred.weather, "is_rough", False) else ""
        bits.append(badge(f"Météo {pred.weather.place} · {pred.weather.summary()}", kind))
    # Une seule alerte par équipe, coupée court : badge, pas paragraphe.
    seen_teams: set[str] = set()
    for flag in pred.news:
        if flag.team in seen_teams:
            continue
        seen_teams.add(flag.team)
        headline = flag.headline.split(" - ")[0][:52].rstrip()
        bits.append(badge(f"Absence · {flag.team} : {headline}…", "warn"))
    if not bits:
        bits.append(badge("Aucun signal de contexte disponible"))
    st.markdown(card("Contexte du match", "".join(bits)), unsafe_allow_html=True)


def render_verdict(pred: Prediction) -> None:
    c1, c2 = st.columns([1.6, 1])
    with c1:
        st.markdown(
            card(
                "Verdict",
                f'<div style="font-size:1rem;line-height:1.55">{esc(pred.verdict)}</div>'
                f'<div class="ps-sub" style="margin-top:.55rem">⚠️ {esc(pred.risk)}</div>',
            ),
            unsafe_allow_html=True,
        )
    with c2:
        conf = pred.confidence
        fig = go.Figure(
            go.Indicator(
                mode="gauge+number", value=conf.score,
                number=dict(suffix=" /10", font=dict(size=26, color=TEXT)),
                gauge=dict(
                    axis=dict(range=[0, 10], tickcolor=MUTED, tickfont=dict(size=10)),
                    bar=dict(color=GOLD, thickness=0.28),
                    bgcolor="rgba(0,0,0,0)", borderwidth=0,
                    steps=[
                        dict(range=[0, 3.5], color="rgba(224,104,95,.18)"),
                        dict(range=[3.5, 6.5], color="rgba(217,180,91,.15)"),
                        dict(range=[6.5, 10], color="rgba(47,211,162,.16)"),
                    ],
                ),
            )
        )
        fig.update_layout(**PLOT_LAYOUT, height=190)
        st.markdown('<div class="ps-card-title">Confiance</div>', unsafe_allow_html=True)
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        if conf.reasons:
            st.markdown(" ".join(badge(r, "warn") for r in conf.reasons[:3]),
                        unsafe_allow_html=True)


def render_quality(result: AnalysisResult) -> None:
    """Ce que l'agent pense de sa propre analyse."""
    assessment = result.decision.assessment
    report = result.research

    tone = (
        "em" if assessment.score >= 0.60
        else ("warn" if assessment.score < 0.40 else "")
    )
    bits = [badge(assessment.label, tone)]
    if report is not None:
        bits.append(badge(f"{len(report.used)} source(s) croisée(s)"))
        if report.duration_s:
            bits.append(badge(f"Recherche en {num(report.duration_s, 1)} s"))
    if result.validation.discarded:
        bits.append(badge(f"{len(result.validation.discarded)} donnée(s) écartée(s)", "warn"))

    rows = "".join(
        f'<div class="ps-vs"><div class="n l" style="font-size:.78rem">'
        f'{pct(value)}</div><div class="k">{esc(label)}</div>'
        f'<div class="r"><div class="ps-bar" style="margin:0">'
        f'<i style="width:{value * 100:.0f}%"></i></div></div></div>'
        for label, value in assessment.as_dict().items()
    )
    body = "".join(bits) + f'<div style="margin-top:.6rem">{rows}</div>'
    for note in assessment.notes[:2]:
        body += f'<div class="ps-sub">⚠ {esc(note)}</div>'
    st.markdown(card("Ce que vaut cette analyse", body), unsafe_allow_html=True)


def render_sources(pred: Prediction) -> None:
    with st.expander("D'où viennent ces informations ?", expanded=False):
        seen, rows = set(), []
        for prov in pred.provenances:
            key = (prov.source, prov.detail)
            if key in seen:
                continue
            seen.add(key)
            tag = (
                badge("données enregistrées", "warn")
                if prov.from_cache
                else badge("mise à jour directe", "em")
            )
            rows.append(
                f'<div class="ps-row"><span class="lbl">{esc(cfg.public_name(prov.source))}'
                f'<span style="color:{MUTED}"> · {esc(prov.detail)}</span></span>'
                f'<span class="val" style="font-weight:400;color:{MUTED}">'
                f"{esc(prov.freshness())} {tag}</span></div>"
            )
        if not rows:
            rows.append(f'<div class="ps-sub">{badge("Aucune source disponible", "warn")}</div>')
        st.markdown("".join(rows), unsafe_allow_html=True)

        report = pred.research
        if report is not None and report.failed:
            st.caption(
                f"{len(report.failed)} source(s) consultée(s) sans réponse — "
                "elles n'ont pas été utilisées."
            )
        st.caption(
            "Comment le résultat est obtenu : les cotes des bookmakers sont "
            "corrigées de leur marge, combinées aux statistiques des équipes "
            f"({cfg.ENGINE.market_weight:.0%} cotes / "
            f"{1 - cfg.ENGINE.market_weight:.0%} statistiques), puis le match est "
            f"rejoué {pred.n_sims:,} fois pour mesurer chaque issue.".replace(",", " ")
        )
        if pred.weather is not None:
            st.caption(
                "La météo est donnée à titre d'information : elle n'entre pas "
                "dans le calcul, faute de mesure fiable de son effet."
            )


def render_debug(result: AnalysisResult) -> None:
    """Mode diagnostic — développement uniquement, jamais visible en production."""
    if not cfg.DEBUG_MODE:
        return

    pred, bundle = result.prediction, result.bundle
    with st.expander("🔧 Diagnostic (développement)", expanded=False):
        st.caption(
            f"Environnement : {cfg.ENVIRONMENT} · empreinte des données "
            f"{result.decision.fingerprint} · analyse en {result.duration_s:.2f} s"
        )

        st.markdown("**Étapes exécutées**")
        st.code(" → ".join(result.steps), language=None)

        st.markdown("**Sources interrogées**")
        report = result.research
        if report is not None:
            st.json(
                {
                    "consultées": report.consulted,
                    "retenues": report.used,
                    "sans réponse": report.failed,
                    "informations trouvées": report.fields_found,
                    "informations manquantes": report.fields_missing,
                    "incohérences": [i.public_text() for i in report.inconsistencies],
                },
                expanded=False,
            )

        st.markdown("**Recherche des cotes**")
        attempts = bundle.odds_diagnostics.attempts
        st.json(
            {
                "résultat": bundle.odds_diagnostics.reason,
                "tentatives": [
                    {"source": a.source, "étape": a.stage, "détail": a.detail}
                    for a in attempts
                ],
            }
            if attempts
            else {"résultat": "aucune source de cotes interrogée"},
            expanded=False,
        )

        st.markdown("**Données validées / écartées**")
        st.json(
            {
                "retenu": result.validation.usable,
                "écarté": [
                    {"champ": f, "motif": r} for f, r in result.validation.discarded
                ],
                "avertissements": result.validation.warnings,
            },
            expanded=False,
        )

        st.markdown("**Valeurs utilisées par le modèle**")
        st.json(
            {
                "simulations": pred.n_sims,
                "moyenne de la compétition": bundle.league_context,
                "attendus": {k: round(v, 4) for k, v in pred.expected.items()},
                "diagnostics du modèle": pred.diagnostics,
                "probabilités": {k: round(v, 4) for k, v in pred.outcome_probs.items()},
                "probabilités du marché": pred.market_probs,
            },
            expanded=False,
        )

        st.markdown("**Facteurs du raisonnement**")
        st.json(
            {
                f.key: {
                    "valeur": round(f.value, 3),
                    "poids": f.weight,
                    "confiance": round(f.confidence, 3),
                    "dans le modèle": f.in_model,
                    "disponible": f.available,
                    "détail": f.detail,
                }
                for f in result.factors.factors
            },
            expanded=False,
        )


def render_history() -> None:
    """Journal des analyses et, quand il y en a assez, leur performance réelle."""
    ledger = get_agent().ledger
    entries = ledger.all()
    if not entries:
        return

    report = PerformanceAnalyst().report(entries)
    with st.expander("Mes analyses précédentes", expanded=False):
        for entry in reversed(entries[-8:]):
            when = entry.created_at[:16].replace("T", " ")
            if entry.resolved:
                mark = badge("réussi", "em") if entry.hit else badge("manqué", "warn")
            else:
                mark = badge("en attente")
            st.markdown(
                f'<div class="ps-row"><span class="lbl">{esc(entry.home)} – '
                f'{esc(entry.away)}<span style="color:{MUTED}"> · '
                f'{esc(entry.competition)}</span></span>'
                f'<span class="val" style="font-weight:400">'
                f'{esc(entry.recommendation)} · {pct(entry.probability)} {mark} '
                f'<span style="color:{MUTED}">{esc(when)}</span></span></div>',
                unsafe_allow_html=True,
            )

        if not report.is_meaningful:
            st.caption(
                f"{report.resolved} résultat(s) connu(s) sur {len(entries)} analyses. "
                "Il en faut au moins 20 pour tirer une conclusion honnête sur la "
                "fiabilité des pronostics."
            )
            return

        st.markdown(
            "".join([
                badge(f"{report.hits}/{report.resolved} pronostics réussis", "em"),
                badge(f"Taux de réussite {pct(report.hit_rate)}"),
                badge(f"Annoncé en moyenne {pct(report.average_predicted)}"),
            ]),
            unsafe_allow_html=True,
        )
        rows = "".join(
            f'<div class="ps-row"><span class="lbl">Annoncé {b.label}</span>'
            f'<span class="val">réalisé {pct(b.observed)} '
            f'<span style="color:{MUTED}">({b.count} cas)</span></span></div>'
            for b in report.bins
        )
        st.markdown(
            card("Les probabilités annoncées se vérifient-elles ?", rows),
            unsafe_allow_html=True,
        )
        render_tuning(report)


def render_tuning(report) -> None:
    """Propositions de réglage — jamais appliquées sans accord explicite."""
    advisor = get_advisor()
    advisor.register(advisor.suggest(report))
    pending = advisor.pending()
    if not pending:
        return

    st.markdown(
        '<div class="ps-card-title" style="margin-top:.8rem">'
        "Ajustements proposés</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        "Rien n'est modifié automatiquement. Un réglage accepté est enregistré "
        "pour que vous le reportiez vous-même dans la configuration."
    )
    for proposal in pending:
        col_text, col_yes, col_no = st.columns([4, 1, 1], vertical_alignment="center")
        with col_text:
            st.markdown(
                f'<div class="ps-row"><span class="lbl">{esc(proposal.rationale)}'
                f'<span style="color:{MUTED}"> — {esc(proposal.evidence)}</span></span>'
                f'<span class="val">{num(proposal.current, 2)} → '
                f"{num(proposal.proposed, 2)}</span></div>",
                unsafe_allow_html=True,
            )
        with col_yes:
            if st.button("Accepter", key=f"ok_{proposal.id}", use_container_width=True):
                advisor.decide(proposal.id, True)
                st.rerun()
        with col_no:
            if st.button("Refuser", key=f"no_{proposal.id}", use_container_width=True):
                advisor.decide(proposal.id, False)
                st.rerun()


# ==========================================================================
# Programme principal
# ==========================================================================
def render_secrets_alert() -> None:
    """Prévient si le fichier de secrets existe mais n'a pas pu être lu.

    Sans ce message, l'application se comporte exactement comme si aucune clé
    n'avait jamais été renseignée : cotes indisponibles, tennis vide, et pas
    la moindre indication que le problème vient de la configuration.
    """
    if not cfg.SECRETS_ERROR:
        return
    st.error(
        "**Vos secrets n'ont pas pu être lus — aucune clé n'est active.**\n\n"
        f"`{cfg.SECRETS_ERROR}`\n\n"
        "Cause quasi systématique : les délimiteurs de bloc de code "
        "(les trois accents graves et le mot `toml`) collés avec le contenu. "
        "Le fichier doit commencer directement par `PRONOSTAT_ENV = \"production\"`.",
        icon="🔑",
    )


def render_config_diagnostic() -> None:
    """Pourquoi aucune clé n'est active — affiché seulement dans ce cas.

    Sans ce panneau, l'absence de clé est indiscernable d'une panne : les
    valeurs par défaut prennent le relais en silence, et l'application se
    comporte comme si tout allait bien. Aucune valeur de clé n'y figure,
    uniquement des noms et des longueurs.
    """
    if cfg.KEYS.odds_api:
        return          # une clé de cotes est active : rien à diagnostiquer

    rapport = cfg.secrets_report()
    with st.expander("🔑  Aucune clé de cotes active — voir pourquoi", expanded=False):
        if not rapport["secrets_lisibles"]:
            st.error(
                "**Le fichier de secrets n'a pas pu être lu.**\n\n"
                f"`{rapport['erreur'] or 'cause inconnue'}`",
                icon="⛔",
            )
        else:
            noms = rapport["noms_dans_secrets"]
            if noms:
                st.success(
                    f"Fichier de secrets lu : **{len(noms)} entrée(s)** — "
                    + ", ".join(f"`{n}`" for n in noms),
                    icon="✅",
                )
            else:
                st.warning(
                    "**Le fichier de secrets est lisible mais vide.** Rien n'a été "
                    "enregistré, ou l'enregistrement s'est fait ailleurs que dans "
                    "*Settings → Secrets* de cette application.",
                    icon="📭",
                )

        st.markdown("**Origine de chaque réglage attendu :**")
        st.table(
            {
                "Réglage": list(rapport["origine"].keys()),
                "Provenance": list(rapport["origine"].values()),
            }
        )
        st.caption(
            "Aucune valeur de clé n'est affichée ici, seulement des noms et des "
            "longueurs. `ODDS_API_KEY` en « ABSENTE » signifie que l'application "
            "ne la voit nulle part : ni dans l'environnement, ni dans les secrets."
        )


def main() -> None:
    drop_stale_caches()
    render_header()
    render_secrets_alert()
    render_config_diagnostic()
    comp, home, away, launch = render_controls()

    if launch and comp and home and away:
        # ---- tout le travail se fait ici, en arrière-plan ----
        # L'agent enchaîne neuf étapes ; l'utilisateur n'en voit que
        # l'avancement, jamais le détail.
        status = st.status("Recherche des informations…", expanded=False)
        result = None
        try:
            status.update(label="Analyse du match en cours…")
            result = get_agent().analyse_match(comp, home, away)
            status.update(label="Analyse terminée", state="complete")
        except Exception:
            status.update(label="Analyse interrompue", state="error")
            st.error(
                "L'analyse n'a pas pu aboutir. Vérifiez votre connexion "
                "internet puis relancez la simulation.",
                icon="⚠️",
            )
        if result is not None:
            st.session_state["result"] = result

    result: AnalysisResult | None = st.session_state.get("result")
    prediction: Prediction | None = result.prediction if result else None

    if prediction is None:
        st.markdown(
            card(
                "Prêt",
                '<div class="ps-sub">Choisissez une compétition, deux adversaires, '
                "puis lancez la simulation. Aucune autre saisie n'est nécessaire.</div>",
            ),
            unsafe_allow_html=True,
        )
    else:
        if prediction.venue_swapped:
            st.caption("Le calendrier réel place l'autre équipe à domicile : ordre corrigé.")
        render_main_pick(result)
        st.write("")
        render_reasoning(result)
        st.write("")
        render_headline(prediction)
        st.write("")
        render_probabilities(prediction)
        st.write("")
        render_markets(prediction)
        st.write("")
        render_scorers(prediction)
        st.write("")
        col_a, col_b = st.columns([1, 1])
        with col_a:
            render_profiles(prediction)
        with col_b:
            render_h2h(prediction)
            st.write("")
            render_score_heatmap(prediction)
        st.write("")
        col_c, col_d = st.columns([1, 1])
        with col_c:
            render_market_comparison(prediction, result.market)
        with col_d:
            render_context(prediction)
            st.write("")
            render_quality(result)
        st.write("")
        render_verdict(prediction)
        st.write("")
        render_sources(prediction)
        render_debug(result)
        render_history()

    st.markdown(
        '<div class="ps-banner">Aucun résultat garanti — jouez responsable. '
        "18+ · Les probabilités sont des estimations, pas des certitudes.</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
