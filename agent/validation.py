"""Module de validation : juger la qualité des données avant de les utiliser.

Responsabilité unique : dire ce qui est exploitable et ce qui ne l'est pas.
Une donnée jugée non fiable est **écartée**, jamais corrigée ni remplacée par
une estimation — l'agent préfère analyser moins que d'analyser faux.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config as cfg
from data_sources import Bundle, TeamForm, name_similarity, normalize_name

UTC = timezone.utc
log = logging.getLogger("pronostat.agent.validation")


@dataclass
class ValidationReport:
    """Ce que la validation a retenu, écarté, et pourquoi."""

    usable: list[str] = field(default_factory=list)
    discarded: list[tuple[str, str]] = field(default_factory=list)  # (champ, motif)
    _warnings: list[str] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        """Avertissements sans doublon : deux équipes produisent souvent le
        même message, et un doublon en masquerait un autre plus important."""
        seen: set[str] = set()
        unique = []
        for message in self._warnings:
            if message not in seen:
                seen.add(message)
                unique.append(message)
        return unique

    def warn(self, message: str) -> None:
        self._warnings.append(message)

    @property
    def coverage(self) -> float:
        total = len(self.usable) + len(self.discarded)
        return len(self.usable) / total if total else 0.0

    def has(self, field_name: str) -> bool:
        return field_name in self.usable


# Bornes de vraisemblance par sport : au-delà, la donnée est aberrante et
# vient presque toujours d'un match qui n'appartient pas à la compétition
# (amical, présaison, forfait). On l'écarte plutôt que de la moyenner.
PLAUSIBLE_SCORE = {
    "football": (0, 12),
    "hockey": (0, 16),
    "basket": (40, 200),
    "tennis": (0, 3),
}


class DataValidator:
    """Contrôle la vraisemblance et la profondeur des données collectées."""

    def validate(self, bundle: Bundle) -> ValidationReport:
        report = ValidationReport()
        sport = bundle.sport

        self._check_form(bundle, "form_home", bundle.form_home, sport, report)
        self._check_form(bundle, "form_away", bundle.form_away, sport, report)
        self._check_competition(bundle, report)
        self._check_odds(bundle, report)
        self._check_standings(bundle, report)
        self._check_h2h(bundle, report)
        self._check_duplicates(bundle, report)
        return report

    # ------------------------------------------------------------------
    def _check_competition(self, bundle: Bundle, report: ValidationReport) -> None:
        """Les matchs récupérés appartiennent-ils bien à la compétition choisie ?

        Une équipe promue n'a pas d'historique dans sa nouvelle division : ses
        résultats viennent alors d'ailleurs. On le signale plutôt que de les
        mélanger silencieusement à ceux d'une autre compétition.
        """
        comp = bundle.competition
        if comp is None:
            return
        for slot, form in (("form_home", bundle.form_home), ("form_away", bundle.form_away)):
            if form is None or not form.matches:
                continue
            labels = {m.competition for m in form.matches if m.competition}
            if not labels:
                continue
            foreign = [
                label for label in labels
                if name_similarity(label, comp.label) < 0.6
            ]
            if foreign and len(foreign) == len(labels):
                report.warn(
                    f"{form.team} : historique issu de « {sorted(labels)[0]} », "
                    f"pas de {comp.label}"
                )

    def _check_duplicates(self, bundle: Bundle, report: ValidationReport) -> None:
        """Un même match ne doit jamais compter deux fois dans un historique."""
        for form in (bundle.form_home, bundle.form_away):
            if form is None or not form.matches:
                continue
            seen: set[tuple] = set()
            unique = []
            for match in form.matches:
                key = (match.date.date(), normalize_name(match.opponent),
                       match.scored, match.conceded)
                if key in seen:
                    continue
                seen.add(key)
                unique.append(match)
            removed = len(form.matches) - len(unique)
            if removed:
                form.matches = unique
                report.warn(
                    f"{removed} match(s) en double retiré(s) de l'historique"
                )

    # ------------------------------------------------------------------
    def _check_form(
        self, bundle: Bundle, slot: str, form: TeamForm | None, sport: str, report
    ) -> None:
        if form is None or form.n == 0:
            report.discarded.append((slot, "aucun match récent trouvé"))
            return

        low, high = PLAUSIBLE_SCORE.get(sport, (0, 999))
        kept = [
            m for m in form.matches
            if low <= m.scored <= high and low <= m.conceded <= high
        ]
        removed = form.n - len(kept)
        if removed:
            form.matches = kept
            report.warn(
                f"{removed} résultat(s) hors normes écarté(s) de l'historique"
            )
            log.debug("%s : %d matchs aberrants écartés", slot, removed)

        if not form.matches:
            report.discarded.append((slot, "tous les résultats étaient aberrants"))
            setattr(bundle, slot, None)
            return

        # Un historique très ancien ne décrit plus l'équipe actuelle.
        newest = max(m.date for m in form.matches)
        age_days = (datetime.now(UTC) - newest).total_seconds() / 86400
        if age_days > 210:
            report.discarded.append((slot, "historique trop ancien"))
            setattr(bundle, slot, None)
            return
        if age_days > 60:
            report.warn("Historique antérieur à la saison en cours")

        if form.n < cfg.ENGINE.min_matches:
            report.warn(
                f"Historique limité à {form.n} match(s) pour {form.team}"
            )
        report.usable.append(slot)

    def _check_odds(self, bundle: Bundle, report: ValidationReport) -> None:
        odds = bundle.odds
        if odds is None or not odds.has_h2h:
            report.discarded.append(("cotes", "aucune cote publiée"))
            return
        implied = sum(1 / price for price in odds.h2h.values() if price > 1)
        # Un livre cohérent affiche une marge entre 0 et 25 %.
        if not 0.98 <= implied <= 1.35:
            report.discarded.append(("cotes", "cotes incohérentes entre elles"))
            bundle.odds = None
            return
        if odds.bookmaker_count < 2:
            report.warn("Un seul bookmaker disponible")
        report.usable.append("cotes")

    def _check_standings(self, bundle: Bundle, report: ValidationReport) -> None:
        if not bundle.standings:
            report.discarded.append(("classement", "classement non publié"))
            return
        played = sum(s.played for s in bundle.standings.values())
        if played < 10:
            report.discarded.append(("classement", "saison trop peu avancée"))
            bundle.standings = {}
            return
        report.usable.append("classement")

    def _check_h2h(self, bundle: Bundle, report: ValidationReport) -> None:
        if not bundle.h2h:
            report.discarded.append(("confrontations", "aucune rencontre commune"))
            return
        recent = [
            m for m in bundle.h2h
            if (datetime.now(UTC) - m.date).days <= 365 * 5
        ]
        if not recent:
            report.discarded.append(("confrontations", "confrontations trop anciennes"))
            bundle.h2h = []
            return
        bundle.h2h = recent
        report.usable.append("confrontations")
