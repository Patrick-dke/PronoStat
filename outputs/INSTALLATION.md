# PronoStat — workflows n8n

Deux fichiers importables, conformes au document `N8N-ARCHITECTURE.md`.

| Fichier | Nœuds | Rôle |
|---|---|---|
| `PronoStat-1-Analyse.n8n.json` | 32 | Découverte → priorisation → recherche → cotes → `POST /analysis` → journal |
| `PronoStat-2-Resultats.n8n.json` | 13 | Scores finaux → `POST /result` → backtesting |

Les 15 nœuds du livrable C sont le squelette logique ; les 32 nœuds réels
ajoutent la validation des clés de compétition, le mode « sans recherche
web », le coupe-circuit de quota et les branches d'erreur. Aucune fonction du document
n'a été retirée.

---

## 1. Prérequis bloquant

**Ces workflows ne peuvent rien faire tant que l'API FastAPI n'existe pas.**
C'est l'étape 1 de votre plan G, et c'est effectivement le vrai prérequis :
Streamlit n'expose pas `POST /analysis`.

Cinq endpoints sont attendus. Les contrats ci-dessous sont ceux que les
workflows émettent et lisent réellement — ils sont exécutables tels quels.

### `GET /quota`

```json
{
  "odds_credits_remaining": 269,
  "odds_credits_limit": 500,
  "period_resets_at": "2026-09-01T00:00:00Z",
  "recent_analyses": [
    { "fixture_key": "soccer_epl|arsenal|coventry|2026-08-21",
      "window": "J-3",
      "analysed_at": "2026-08-18T09:12:00Z" }
  ]
}
```

`recent_analyses` : les 30 derniers jours suffisent. C'est cette liste qui
évite de repayer des cotes pour une fenêtre déjà couverte.
Si `odds_credits_remaining` est absent ou non numérique, **le cycle entier
est reporté** — aucun crédit n'est engagé sur une estimation.

### `POST /analysis`

Reçoit exactement le schéma du livrable D, augmenté de `quality` et
`contradictions` (produits par l'agent 5). Doit répondre :

```json
{
  "analysis_id": "…", "model_version": "2.4.1",
  "main_pick": { … }, "outcome_probs": { … }, "markets": { … },
  "top_scores": [ … ], "pick_scores": { … },
  "confidence": 7.2, "consistency": [], "sources": [ … ]
}
```

`consistency` **non vide ⇒ publication bloquée**. Le tableau vide est la
réponse normale ; ne renvoyez pas `null`.

### `GET /analysis/pending`

```json
[ { "analysis_id": "…", "fixture_key": "…", "event_id": "…",
    "home": "…", "away": "…", "starts_at": "…", "competition_key": "…" } ]
```

### `GET /score?fixture_key=…&event_id=…`

```json
{ "status": "finished", "home_goals": 2, "away_goals": 1,
  "finished_at": "…", "source": "thesportsdb" }
```

Enveloppe `DataHub.final_score()`. Tout `status` autre que `"finished"` est
traité comme « score indisponible » : l'analyse reste en attente. **Un score
introuvable n'est jamais compté comme un échec** — le compter fausserait le
Brier et la calibration.

### `POST /result`

`{analysis_id, home_goals, away_goals, finished_at}` → déclenche
`resolve_pending()` et le recalcul de backtesting.

---

## 2. Import

1. n8n → **Workflows** → *Import from File* → les deux `.json`.
2. Créer les credentials ci-dessous, puis les rattacher dans chaque nœud
   marqué en rouge (le nom pré-rempli indique lequel).
3. Nœud **Config** (workflow 1) et **Config résultats** (workflow 2) :
   remplacer `https://pronostat-api.example.com` par votre URL réelle.
4. Nœud **Journal Firestore** : sélectionner le projet dans la liste
   déroulante (le champ contient `VOTRE_PROJET_FIREBASE`).
5. Activer le workflow 1 seul pendant 3 jours avant d'activer le workflow 2.

### Credentials à créer

| Nom exact | Type n8n | Réglage |
|---|---|---|
| `The Odds API — apiKey (query)` | Query Auth | Name `apiKey`, Value = votre clé |
| `Serper — X-API-KEY` | Header Auth | Name `X-API-KEY`, Value = votre clé |
| `Anthropic API` | Anthropic | votre clé API |
| `Firestore — PronoStat` | Google Firebase Cloud Firestore OAuth2 | compte du projet |

Aucune clé n'est écrite dans les fichiers. Le nœud Config ne contient que
des paramètres de pilotage.

---

## 3. Ce que le quota permet réellement

Chiffres de votre document : 269 crédits restants, réserve de 30 → **239
crédits utilisables**. Une affiche coûte 3 crédits.

> **Le quota achète 79 analyses d'ici la remise à zéro. Pas 80.**
> La seule question est comment les répartir.

Deux politiques, commutables par `allocationMode` dans le nœud Config :

| Mode | Règle | Répartition des 79 analyses |
|---|---|---|
| `share` *(défaut, fidèle au livrable B)* | ≤ 20 % du restant par cycle | **47 le 1ᵉʳ jour**, 74 au 3ᵉ, épuisé au 7ᵉ |
| `pace` *(recommandé)* | budget utilisable ÷ temps avant remise à zéro, avec accumulation des fractions | ≈ 3,4 par jour, régulier sur 23 jours |

`share` avec 4 cycles par jour consomme 59 % du budget dès le premier jour
— la décroissance est géométrique en 0,8ⁿ. Le mois se joue en une semaine.
`pace` applique au temps exactement l'argument de votre document : *« la
valeur marginale d'une quatrième actualisation est faible face à une
première analyse sur un match non couvert »*.

`pace` exige `period_resets_at` dans `GET /quota`. S'il est absent, le nœud
**annonce le repli** sur `share` dans les logs plutôt que de deviner la date.

**Serper n'est pas la contrainte.** 79 matchs × 10 recherches = 790 requêtes,
soit moins d'un tiers du palier gratuit. Le poste critique est, et reste,
The Odds API.

---

## 4. Écarts assumés par rapport au document de conception

Six corrections. Chacune est un choix technique, pas une omission.

**1 — Le coût des cotes n'est pas forfaitaire.**
The Odds API facture `nb_marchés × nb_régions`, pas `nb_marchés`. Les trois
marchés sur deux régions coûteraient **6 crédits**, pas 3, et diviseraient
par deux le nombre d'affiches analysables. `oddsRegions` est donc verrouillé
à une seule valeur (`eu`) et le commentaire du nœud Config l'explique.
Vérifiez ce point avant d'ajouter `uk` ou `us`.

**2 — Les clés de compétition sont vérifiées avant toute dépense.**
Un nœud interroge `GET /v4/sports` (**gratuit, 0 crédit**) et ne conserve
que les clés réellement actives. The Odds API ajoute et retire des clés au
fil des saisons ; une clé périmée aurait produit un appel payant sans
résultat. Les clés écartées sont journalisées, jamais devinées.

**3 — La fraîcheur est mesurée en fenêtres, pas en heures.**
Le nœud 6 du livrable C saute une analyse de moins de 24 h. Un match vu à
J-3 il y a 25 h serait donc ré-analysé pour rien. La règle retenue : on
ré-analyse **quand le match change de fenêtre** (J-7 → J-3 → J-1 →
PRE_MATCH), avec le plancher de 24 h conservé en garde-fou. Économie directe
de crédits, sans perte d'information.

**4 — Un coupe-circuit remplace le nœud 6 dans la boucle.**
Le budget est calculé au démarrage du cycle ; une exécution longue peut le
périmer. Avant chaque match, le workflow relit le compteur réel renvoyé par
The Odds API (en-tête `x-requests-remaining`, persisté entre exécutions) et
s'arrête si la réserve est menacée — **avant** de dépenser des requêtes
Serper sur un match qu'il ne pourra pas coter.

**5 — Le mode « sans recherche web » est une branche, pas un réglage.**
L'étape 3 de votre plan G demande un workflow minimal *sans recherche web*.
Un simple `searchesPerMatch: 0` aurait vidé la branche et bloqué la boucle
— un nœud Code qui ne renvoie rien arrête son chemin dans n8n. Le workflow
comporte donc un aiguillage explicite (`Recherche web activée ?`) et un
point de convergence (`Dossier de recherche`). L'étape 3 est exécutable
telle quelle, et le retour à l'étape 5 se fait en changeant un seul chiffre.

**6 — Une analyse bloquée est journalisée quand même.**
Le livrable E dit « publication bloquée, alerte ». Les workflows bloquent la
publication **et** écrivent l'analyse dans Firestore avec
`publiable: false` et le motif. Une analyse incohérente reste une trace
d'audit et une donnée de backtesting ; la perdre serait une faute.

Le nœud d'alerte est un point d'accroche vide : vous n'avez spécifié aucun
canal, je n'en ai supposé aucun. Branchez-y Telegram, Slack ou un e-mail.

---

## 5. Ce que je n'ai pas pu vérifier

Par honnêteté, et parce que ces points peuvent casser à l'exécution :

- **Les 18 clés de compétition** du nœud Config viennent de ma connaissance
  de The Odds API, pas d'un appel réel — je n'ai pas votre clé. C'est
  précisément pourquoi l'écart n°2 existe : le workflow les valide lui-même
  au premier cycle. Lisez les logs du nœud « Compétitions retenues ».
- **L'en-tête `x-requests-last`** (coût du dernier appel) est lu de façon
  défensive : s'il n'existe pas, le champ vaut `null` et rien ne casse.
  `x-requests-remaining`, lui, est documenté et fiable.
- **Le palier Serper à 2 500 requêtes/mois** vient de votre document. Je ne
  l'ai pas revérifié aujourd'hui.
- **La version de votre n8n.** Les `typeVersion` sont volontairement
  conservateurs (`httpRequest` 4.2, `code` 2, `if` 2, `splitInBatches` 3,
  `scheduleTrigger` 1.2). Si votre instance est antérieure à n8n 1.40,
  vérifiez l'import avant de compter dessus.
- **Le champ `projectId` du nœud Firestore** peut être un sélecteur selon
  votre version : sélectionnez le projet dans l'interface après import.

---

## 6. Vérification effectuée

Les fichiers livrés ne sont pas seulement bien formés — les 21 nœuds Code
ont été exécutés hors n8n contre des données factices.

```
python3 _validate.py    # structure : connexions, orphelins, index de sortie,
                        # expressions, références $('Nœud'), syntaxe JS
node _dryrun.js         # workflow 1 — 62 assertions
node _dryrun2.js        # workflow 2 — 11 assertions
```

Cas couverts, entre autres : quota illisible, quota au plancher, doublon
d'affiche à noms approchants, même paire à deux dates différentes, réponse
LLM illisible, statut inventé par le LLM, valeur « disponible » sans source,
contradiction entre sources, cotes vides, score 0-0 (piège du *falsy*),
match encore en cours, moteur injoignable, incohérence détectée.

Le banc d'essai a trouvé un bug réel avant livraison : une classe de
caractères mal échappée qui transformait « Boston Celtics » en « oston
eltics ». Corrigé.

Ce que ces tests **ne** couvrent **pas** : le comportement réel des API
tierces, la résolution des références `$('Nœud')` par n8n à l'intérieur
d'une boucle, et la conformité de votre FastAPI aux contrats du §1. Faites
tourner le workflow 1 en manuel une fois avant de l'activer.

---

## 7. Ordre de mise en service

Conforme à votre plan G — l'étape 3 avant l'étape 5 est la bonne décision.

1. FastAPI en ligne, les cinq endpoints répondent.
2. Workflow 1 en **exécution manuelle**, une fois. Vérifier les logs du nœud
   « Priorisation » : budget, créneaux, motifs d'exclusion.
3. Pour valider l'orchestration sans payer la couche recherche : dans
   Config, mettre `searchesPerMatch: 0`. Le workflow emprunte la branche
   **« Recherche désactivée »** : les huit champs sont marqués `non_trouve`
   avec leur motif, le moteur travaille sur ses seules sources internes.
   C'est le repli du livrable E, câblé explicitement — pas un contournement.
4. Trois jours sous surveillance. Le quota ne doit jamais franchir la
   réserve.
5. Activer le workflow 2.
6. Remettre `searchesPerMatch: 10`, d'abord sur une seule compétition
   (réduire la liste `competitions`), et comparer les niveaux de confiance
   avec et sans recherche.
7. Étendre progressivement.
