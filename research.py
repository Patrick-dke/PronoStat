"""Moteur de recherche approfondie de PronoStat.

Rôle : interroger **toutes** les sources autorisées disponibles, en parallèle,
puis fusionner ce qu'elles renvoient en une vérité unique et traçable.

    sources multiples ──► collecte parallèle ──► fusion ──► dédoublonnage
                                                    │
                                                    ├─► la plus récente gagne
                                                    ├─► la plus fiable arbitre
                                                    ├─► incohérences détectées
                                                    └─► indice de fiabilité

Tout se passe en arrière-plan : l'interface ne voit qu'un `Bundle` complet et
un `ResearchReport` résumé. Aucune donnée n'est jamais inventée — une source
absente laisse le champ vide, et le rapport le dit.

Aucune source n'est interrogée hors de ses conditions d'utilisation : seules
des API officielles, des services publics (MediaWiki, Wikidata, Open-Meteo) et
des jeux de données librement réutilisables sont sollicités. Aucun scraping.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

import config as cfg
from config import Competition
from data_sources import (
    BaseProvider,
    Bundle,
    MatchResult,
    Provenance,
    Standing,
    TeamForm,
    name_similarity,
    normalize_name,
)

UTC = timezone.utc
log = logging.getLogger("pronostat.research")


# ==========================================================================
# Résultats de la fusion
# ==========================================================================
@dataclass
class SourceClaim:
    """Ce qu'une source affirme, avec de quoi l'arbitrer."""

    source: str
    value: Any
    provenance: Provenance

    @property
    def reliability(self) -> float:
        return cfg.reliability(self.source)

    @property
    def freshness_weight(self) -> float:
        """1.0 pour une donnée fraîche, décroît jusqu'à 0.5 après 7 jours."""
        days = self.provenance.age_seconds / 86400.0
        return max(0.5, 1.0 - 0.07 * days)

    @property
    def score(self) -> float:
        return self.reliability * self.freshness_weight


@dataclass
class Inconsistency:
    """Deux sources se contredisent sur une même grandeur."""

    field: str
    detail: str
    sources: tuple[str, ...]
    severity: float = 0.5   # 0 = anecdotique, 1 = majeur

    def public_text(self) -> str:
        return self.detail


@dataclass
class RosterResult:
    """Effectif consolidé d'une compétition."""

    names: list[str]
    sources: list[Provenance] = field(default_factory=list)
    expected: int = 0
    confirmed: int = 0          # noms vus par au moins deux sources
    unconfirmed: list[str] = field(default_factory=list)
    season: int | None = None   # saison retenue (année de départ)

    @property
    def season_label(self) -> str:
        return cfg.season_label(self.season) if self.season else ""

    @property
    def coverage(self) -> float | None:
        """Part de l'effectif attendu réellement retrouvée."""
        if not self.expected:
            return None
        return min(1.0, len(self.names) / self.expected)

    @property
    def is_complete(self) -> bool:
        return self.coverage is not None and self.coverage >= 0.98

    @property
    def reliability(self) -> float:
        """Indice global : meilleure source × couverture × taux de confirmation."""
        if not self.names:
            return 0.0
        best = max((cfg.reliability(p.source) for p in self.sources), default=0.4)
        coverage = self.coverage if self.coverage is not None else 0.85
        confirmed_ratio = self.confirmed / len(self.names)
        return round(best * (0.55 + 0.25 * coverage + 0.20 * confirmed_ratio), 3)


@dataclass
class ResearchReport:
    """Synthèse de la collecte, pour l'affichage et le calcul de confiance."""

    competition: Competition
    consulted: list[str] = field(default_factory=list)     # sources interrogées
    used: list[str] = field(default_factory=list)          # sources retenues
    failed: list[str] = field(default_factory=list)        # sources muettes
    inconsistencies: list[Inconsistency] = field(default_factory=list)
    fields_found: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    roster: RosterResult | None = None

    @property
    def coverage(self) -> float:
        """Part des informations recherchées effectivement trouvées."""
        total = len(self.fields_found) + len(self.fields_missing)
        return len(self.fields_found) / total if total else 0.0

    @property
    def reliability(self) -> float:
        """Indice de fiabilité global de la collecte, entre 0 et 1."""
        if not self.used:
            return 0.0
        weights = [cfg.reliability(s) for s in self.used]
        base = sum(weights) / len(weights)
        # Une source de plus qui confirme renforce la confiance, mais avec
        # des rendements décroissants.
        breadth = min(1.0, 0.70 + 0.10 * len(self.used))
        penalty = sum(i.severity for i in self.inconsistencies) * 0.08
        return round(max(0.0, min(1.0, base * breadth * self.coverage - penalty)), 3)

    @property
    def label(self) -> str:
        score = self.reliability
        if score >= 0.75:
            return "Très fiable"
        if score >= 0.55:
            return "Fiable"
        if score >= 0.35:
            return "Partielle"
        return "Limitée"


# ==========================================================================
# Exécution parallèle tolérante aux pannes
# ==========================================================================
def gather(
    tasks: dict[str, Callable[[], Any]], max_workers: int | None = None
) -> dict[str, Any]:
    """Exécute des appels indépendants en parallèle.

    Une tâche qui échoue renvoie `None` : une source en panne ne doit jamais
    interrompre la collecte. Les erreurs sont journalisées, jamais affichées.
    """
    if not tasks:
        return {}
    workers = min(len(tasks), max_workers or cfg.MAX_PARALLEL_FETCHES)
    results: dict[str, Any] = {key: None for key in tasks}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:  # une source ne casse jamais l'application
                log.debug("source %s en échec : %s", key, exc)
                results[key] = None
    return results


# ==========================================================================
# Moteur
# ==========================================================================
class DeepResearch:
    """Collecte multi-sources, fusion et contrôle de cohérence."""

    def __init__(self, hub):
        self.hub = hub

    # ------------------------------------------------------------------
    # Effectifs complets
    # ------------------------------------------------------------------
    def roster(self, comp: Competition) -> RosterResult:
        """Effectif complet d'une compétition, fusionné depuis toutes les sources.

        Trois règles, dans cet ordre :

        1. **Une seule saison.** À l'intersaison, les sources ne basculent pas
           toutes en même temps sur le nouvel exercice. On retient la saison la
           plus récente annoncée, et on écarte les sources restées sur la
           précédente : sinon on mélangerait promus et relégués.
        2. **Inclusion prudente.** Un nom est retenu s'il est confirmé par au
           moins deux sources, ou s'il vient d'une source de référence.
        3. **Complétion de dernier recours.** Si l'effectif reste manifestement
           incomplet, on accepte les noms isolés plutôt que d'amputer la liste.
        """
        providers = [p for p in self.hub.providers if self._can_list(p, comp)]
        results = gather({p.name: self._safe(p.participants, comp) for p in providers})

        claims_by_source: dict[str, tuple[list[str], Provenance]] = {
            source: got for source, got in results.items() if got
        }
        if not claims_by_source:
            return RosterResult(names=[], expected=comp.expected_teams)

        # --- 1. n'garder que la saison la plus récente ---
        seasons = [
            prov.season
            for _names, prov in claims_by_source.values()
            if prov.season is not None
        ]
        target_season = max(seasons) if seasons else None
        if target_season is not None:
            claims_by_source = {
                source: (names, prov)
                for source, (names, prov) in claims_by_source.items()
                if prov.season is None or prov.season == target_season
            }

        # --- regroupement par nom normalisé ---
        by_key: dict[str, list[SourceClaim]] = {}
        sources: list[Provenance] = []
        for source, (names, prov) in claims_by_source.items():
            sources.append(prov)
            for raw in names:
                key = normalize_name(raw)
                if key:
                    by_key.setdefault(key, []).append(SourceClaim(source, raw, prov))

        # --- 2. arbitrage : le libellé vient de la source la plus fiable ---
        # Quand une source date explicitement sa saison, elle seule peut
        # introduire une équipe : les sources sans saison (bases généralistes
        # listant aussi d'anciens membres) ne servent qu'à confirmer.
        dated_sources = {
            source
            for source, (_names, prov) in claims_by_source.items()
            if prov.season is not None
        }
        # Cas particulier : une seule source a répondu. Aucun recoupement n'est
        # possible, mais afficher sa liste vaut mieux que ne rien proposer —
        # l'indice de fiabilité, lui, restera bas.
        lone_source = len(claims_by_source) == 1

        confirmed: dict[str, str] = {}
        tentative: dict[str, tuple[str, float]] = {}
        for key, claims in by_key.items():
            best = max(claims, key=lambda c: c.score)
            distinct_sources = {c.source for c in claims}
            if lone_source:
                confirmed[key] = best.value
            elif dated_sources and not (distinct_sources & dated_sources):
                tentative[key] = (best.value, best.score)
            elif len(distinct_sources) >= 2 or best.reliability >= 0.80:
                confirmed[key] = best.value
            else:
                tentative[key] = (best.value, best.score)

        names = sorted(confirmed.values())
        unconfirmed = sorted(value for value, _s in tentative.values())

        # --- 3. complétion de dernier recours ---
        # Uniquement quand il ne manque que quelques équipes : si la liste est
        # très incomplète, c'est que la collecte a échoué, et la compléter avec
        # des noms non confirmés ajouterait du bruit plutôt que de l'information.
        if (
            comp.expected_teams
            and tentative
            and 0.6 * comp.expected_teams <= len(names) < comp.expected_teams
        ):
            extra = sorted(tentative.values(), key=lambda item: -item[1])
            missing = comp.expected_teams - len(names)
            names = sorted(names + [value for value, _s in extra[:missing]])

        return RosterResult(
            names=names,
            sources=sources,
            expected=comp.expected_teams,
            confirmed=len(confirmed),
            unconfirmed=unconfirmed,
            season=target_season,
        )

    # ------------------------------------------------------------------
    # Dossier complet d'un match
    # ------------------------------------------------------------------
    def investigate(
        self,
        comp: Competition,
        home: str,
        away: str,
        with_news: bool = True,
        with_weather: bool = True,
    ) -> tuple[Bundle, ResearchReport]:
        """Rassemble tout ce qui est connu sur un match, toutes sources confondues."""
        started = datetime.now(UTC)
        bundle = Bundle(sport=comp.sport, home=home, away=away, competition=comp)
        report = ResearchReport(competition=comp)

        providers = [p for p in self.hub.sources_for(comp)]
        report.consulted = [p.name for p in providers]

        # --- 1. tous les appels indépendants partent en même temps ---
        tasks: dict[str, Callable[[], Any]] = {}
        odds_sources = 0
        for provider in providers:
            if provider.provides_odds:
                odds_sources += 1
                # Le diagnostic est partagé : chaque source y consigne
                # pourquoi elle a — ou n'a pas — trouvé de cotes.
                tasks[f"odds::{provider.name}"] = self._safe(
                    provider.odds, comp, home, away, bundle.odds_diagnostics
                )
            tasks[f"form_home::{provider.name}"] = self._safe(provider.form, comp, home)
            tasks[f"form_away::{provider.name}"] = self._safe(provider.form, comp, away)
            tasks[f"standings::{provider.name}"] = self._safe(provider.standings, comp)
            tasks[f"h2h::{provider.name}"] = self._safe(
                provider.head_to_head, comp, home, away
            )
            if with_news:
                tasks[f"news_home::{provider.name}"] = self._safe(
                    provider.news, comp, home
                )
                tasks[f"news_away::{provider.name}"] = self._safe(
                    provider.news, comp, away
                )

        raw = gather(tasks)

        # --- 2. fusion, champ par champ ---
        self._merge_odds(bundle, report, raw)
        self._merge_form(bundle, report, raw, "form_home", "form_home")
        self._merge_form(bundle, report, raw, "form_away", "form_away")
        self._merge_standings(bundle, report, raw)
        self._merge_h2h(bundle, report, raw)
        self._merge_news(bundle, report, raw)

        # --- 3. enrichissements dépendants (une fois les cotes connues) ---
        if with_weather:
            self._attach_weather(bundle, report)

        # --- 4. contrôles croisés ---
        self._cross_check(bundle, report)

        # --- 5. contexte de compétition et synthèse ---
        bundle.league_context = self.hub.league_context(comp, bundle)
        report.used = sorted({p.source for p in bundle.provenances})
        report.failed = sorted(set(report.consulted) - set(report.used))
        report.duration_s = (datetime.now(UTC) - started).total_seconds()
        return bundle, report

    # ------------------------------------------------------------------
    # Fusion par champ
    # ------------------------------------------------------------------
    def _merge_odds(self, bundle: Bundle, report: ResearchReport, raw: dict) -> None:
        claims = [
            (source, value)
            for key, value in raw.items()
            if key.startswith("odds::") and value is not None
            for source in [key.split("::", 1)[1]]
            if getattr(value, "has_h2h", False)
        ]
        if not claims:
            report.fields_missing.append("cotes")
            bundle.notes.append("odds_missing")
            # Faute de cotes en direct, on cherche un repère dans les cotes de
            # clôture de la saison. C'est un ancrage plus faible, mais très
            # supérieur à une analyse sans aucune référence de marché.
            self._attach_market_reference(bundle, report)
            # Aucune source de cotes n'a même été interrogée : on le dit.
            if not bundle.odds_diagnostics.attempts:
                bundle.odds_diagnostics.add("aucune", "no_key")
            log.info(
                "aucune cote pour %s vs %s : %s",
                bundle.home, bundle.away, bundle.odds_diagnostics.reason,
            )
            return
        # Le marché le mieux fourni en bookmakers l'emporte.
        source, snapshot = max(claims, key=lambda item: item[1].bookmaker_count)
        bundle.odds = snapshot
        bundle.track(snapshot.provenance)
        report.fields_found.append("cotes")

    def _attach_market_reference(self, bundle: Bundle, report: ResearchReport) -> None:
        comp = bundle.competition
        getter = getattr(self.hub, "market_reference", None)
        if comp is None or getter is None:
            return
        try:
            got = getter(comp, bundle.home, bundle.away)
        except Exception as exc:
            log.debug("repère de marché indisponible : %s", exc)
            got = None
        if not got:
            return
        probs, prov, detail = got
        bundle.market_reference = probs
        bundle.market_reference_detail = detail
        bundle.track(prov)
        report.fields_found.append("repère de marché")
        log.info(
            "repère de marché pour %s vs %s : %s",
            bundle.home, bundle.away,
            ", ".join(f"{k}={v:.0%}" for k, v in probs.items()),
        )

    def _merge_form(
        self, bundle: Bundle, report: ResearchReport, raw: dict, prefix: str, slot: str
    ) -> None:
        claims: list[SourceClaim] = []
        for key, value in raw.items():
            if not key.startswith(f"{prefix}::") or value is None:
                continue
            if getattr(value, "n", 0) <= 0:
                continue
            claims.append(SourceClaim(key.split("::", 1)[1], value, value.provenance))

        if not claims:
            report.fields_missing.append(slot)
            if "form_missing" not in bundle.notes:
                bundle.notes.append("form_missing")
            return

        # Un historique deux fois plus long vaut mieux qu'une source un peu
        # plus fiable : on pondère la fiabilité par la profondeur obtenue.
        def merit(claim: SourceClaim) -> float:
            depth = min(1.0, claim.value.n / max(1, cfg.FORM_WINDOW))
            richness = 0.15 if claim.value.xg_for is not None else 0.0
            return claim.score * (0.55 + 0.45 * depth) + richness

        best = max(claims, key=merit)
        chosen: TeamForm = best.value

        # Complétion : si une autre source apporte des statistiques que la
        # source retenue n'a pas (xG, corners…), on les récupère.
        for claim in claims:
            if claim is best:
                continue
            self._backfill_stats(chosen, claim.value)

        # Second étage : les agrégats de saison. La complétion ci-dessus
        # rapproche les rencontres une à une ; une source qui ne publie
        # qu'une moyenne de saison ne peut pas l'emprunter, alors qu'elle
        # apporte souvent la seule donnée disponible — les corners, par
        # exemple, jusqu'ici absents de toutes les sources gratuites.
        equipe = bundle.home if slot == "form_home" else bundle.away
        self._backfill_season(chosen, bundle.competition, equipe)

        setattr(bundle, slot, chosen)
        bundle.track(chosen.provenance)
        report.fields_found.append(slot)

        # Désaccord entre sources sur le volume de buts marqués.
        self._flag_form_disagreement(report, slot, claims)

    def _backfill_season(self, target: TeamForm, comp, team: str) -> None:
        """Dépose les moyennes de saison absentes du détail par match.

        Ne remplace jamais une statistique déjà connue rencontre par
        rencontre : celle-ci décrit la forme récente, la moyenne de saison
        seulement la tendance générale. Écraser l'une par l'autre ferait
        passer un chiffre de fond pour un chiffre d'actualité.
        """
        if comp is None:
            return
        for provider in self.hub.providers:
            if not hasattr(provider, "season_profile"):
                continue
            try:
                profil = provider.season_profile(comp, team)
            except Exception:
                continue
            if not profil:
                continue
            deja_vu = {c for m in target.matches for c in m.extra}
            ajoutes = 0
            for cle, valeur in profil.items():
                if cle not in deja_vu and cle not in target.extra:
                    target.extra[cle] = valeur
                    ajoutes += 1
            if ajoutes:
                target.extra["season_stats_source"] = provider.name
            return          # une seule source d'agrégat suffit

    @staticmethod
    def _backfill_stats(target: TeamForm, other: TeamForm) -> None:
        """Récupère sur `other` les statistiques absentes de `target`."""
        if not other.matches:
            return
        index = {
            (m.date.date(), normalize_name(m.opponent)): m for m in other.matches
        }
        enriched = 0
        for match in target.matches:
            twin = index.get((match.date.date(), normalize_name(match.opponent)))
            if twin is None:
                continue
            for key, value in twin.extra.items():
                if key not in match.extra:
                    match.extra[key] = value
                    enriched += 1
        if enriched:
            target.extra.setdefault("backfilled_stats", 0)
            target.extra["backfilled_stats"] += enriched

    @staticmethod
    def _flag_form_disagreement(
        report: ResearchReport, slot: str, claims: list[SourceClaim]
    ) -> None:
        averages = {
            c.source: c.value.scored_avg
            for c in claims
            if c.value.scored_avg is not None and c.value.n >= 3
        }
        if len(averages) < 2:
            return
        low, high = min(averages.values()), max(averages.values())
        if high <= 0:
            return
        gap = (high - low) / high
        if gap > 0.35:
            report.inconsistencies.append(
                Inconsistency(
                    field=slot,
                    detail="Deux sources ne rapportent pas les mêmes résultats récents",
                    sources=tuple(sorted(averages)),
                    severity=min(1.0, gap),
                )
            )

    def _merge_standings(self, bundle: Bundle, report: ResearchReport, raw: dict) -> None:
        claims: list[SourceClaim] = []
        for key, value in raw.items():
            if not key.startswith("standings::") or not value:
                continue
            table, prov = value
            if table:
                claims.append(SourceClaim(key.split("::", 1)[1], table, prov))
        if not claims:
            report.fields_missing.append("classement")
            return
        best = max(claims, key=lambda c: (len(c.value), c.score))
        bundle.standings = best.value
        bundle.track(best.provenance)
        report.fields_found.append("classement")

        for form, team in ((bundle.form_home, bundle.home), (bundle.form_away, bundle.away)):
            standing = bundle.standing(team)
            if form is not None and standing is not None:
                form.extra.update(
                    rank=standing.rank,
                    points_per_game=standing.points_per_game,
                    played=standing.played,
                )

    def _merge_h2h(self, bundle: Bundle, report: ResearchReport, raw: dict) -> None:
        claims: list[SourceClaim] = []
        for key, value in raw.items():
            if not key.startswith("h2h::") or not value:
                continue
            matches, prov = value
            if matches:
                claims.append(SourceClaim(key.split("::", 1)[1], matches, prov))

        if claims:
            best = max(claims, key=lambda c: (len(c.value), c.score))
            bundle.h2h = self._dedupe_matches(best.value)
            bundle.track(best.provenance)
            report.fields_found.append("confrontations")
            return

        # Repli : déduire les confrontations de l'historique déjà collecté.
        if bundle.form_home:
            derived = [
                m
                for m in bundle.form_home.matches
                if name_similarity(m.opponent, bundle.away) >= 0.8
            ]
            if derived:
                bundle.h2h = self._dedupe_matches(derived)
                report.fields_found.append("confrontations")
                return
        report.fields_missing.append("confrontations")

    @staticmethod
    def _dedupe_matches(matches: Sequence[MatchResult]) -> list[MatchResult]:
        """Élimine les doublons (même date, même adversaire) entre sources."""
        seen: set[tuple] = set()
        out: list[MatchResult] = []
        for match in sorted(matches, key=lambda m: m.date, reverse=True):
            key = (match.date.date(), normalize_name(match.opponent),
                   match.scored, match.conceded)
            if key in seen:
                continue
            seen.add(key)
            out.append(match)
        return out

    def _merge_news(self, bundle: Bundle, report: ResearchReport, raw: dict) -> None:
        seen: set[str] = set()
        for key, value in raw.items():
            if not key.startswith(("news_home::", "news_away::")) or not value:
                continue
            for flag in value:
                signature = normalize_name(flag.headline)[:80]
                if signature in seen:
                    continue
                seen.add(signature)
                bundle.news.append(flag)
                bundle.track(flag.provenance)
        if bundle.news:
            report.fields_found.append("actualité")
        else:
            report.fields_missing.append("actualité")

    def _attach_weather(self, bundle: Bundle, report: ResearchReport) -> None:
        comp = bundle.competition
        weather_provider = getattr(self.hub, "weather_provider", None)
        if comp is None or weather_provider is None or not weather_provider.handles(comp):
            return
        if bundle.odds is None or bundle.odds.commence_time is None:
            report.fields_missing.append("météo")
            return
        try:
            place = self.hub.sportsdb.venue(comp, bundle.odds.home_team or bundle.home)
            weather = (
                weather_provider.forecast(comp, place, bundle.odds.commence_time)
                if place
                else None
            )
        except Exception as exc:
            log.debug("météo indisponible : %s", exc)
            weather = None
        if weather:
            bundle.weather = weather
            bundle.track(weather.provenance)
            report.fields_found.append("météo")
        else:
            report.fields_missing.append("météo")

    # ------------------------------------------------------------------
    # Contrôles croisés
    # ------------------------------------------------------------------
    def _cross_check(self, bundle: Bundle, report: ResearchReport) -> None:
        """Repère les contradictions entre données provenant de sources différentes."""
        # 1. Le classement contredit-il franchement la forme récente ?
        for form, team in ((bundle.form_home, bundle.home), (bundle.form_away, bundle.away)):
            standing = bundle.standing(team)
            if form is None or standing is None or standing.played < 5 or form.n < 4:
                continue
            season_rate = standing.points / (3 * standing.played)
            recent_rate = form.points_rate
            if recent_rate is None:
                continue
            if abs(season_rate - recent_rate) > 0.45:
                report.inconsistencies.append(
                    Inconsistency(
                        field="forme",
                        detail=f"{team} : la forme récente s'écarte nettement de sa saison",
                        sources=("classement", "forme"),
                        severity=0.35,
                    )
                )

        # 2. Les bookmakers sont-ils d'accord entre eux ?
        if bundle.odds is not None:
            dispersion = bundle.odds.dispersion
            if dispersion is not None and dispersion > 0.10:
                report.inconsistencies.append(
                    Inconsistency(
                        field="cotes",
                        detail="Les bookmakers ne s'accordent pas sur ce match",
                        sources=("cotes",),
                        severity=min(1.0, dispersion * 3),
                    )
                )

        # 3. Des données périmées se sont-elles glissées dans le dossier ?
        stale = [p for p in bundle.provenances if p.is_stale]
        if stale:
            report.inconsistencies.append(
                Inconsistency(
                    field="fraîcheur",
                    detail=f"{len(stale)} information(s) datent de plus de deux jours",
                    sources=tuple(sorted({p.source for p in stale})),
                    severity=0.3,
                )
            )

    # ------------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------------
    @staticmethod
    def _can_list(provider: BaseProvider, comp: Competition) -> bool:
        try:
            return bool(provider.enabled) and provider.handles(comp)
        except Exception:
            return False

    @staticmethod
    def _safe(method: Callable, *args) -> Callable[[], Any]:
        """Emballe un appel de source pour qu'il ne puisse jamais lever."""

        def runner():
            try:
                return method(*args)
            except Exception as exc:
                log.debug("appel %s en échec : %s", getattr(method, "__qualname__", method), exc)
                return None

        return runner
