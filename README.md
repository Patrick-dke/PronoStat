# 🎯 PronoStat

**Agent d'analyse sportive autonome**, spécialisé dans les compétitions
majeures : les cinq grands championnats européens et les coupes d'Europe, la
NBA/WNBA, la NHL, et les circuits ATP/WTA.

Ce n'est ni un chatbot ni un assistant conversationnel : c'est un **moteur de
décision**. On lui désigne une rencontre, il va chercher les données, les
vérifie, les recoupe, raisonne, puis produit **une** recommandation assortie de
sa probabilité, de son niveau de confiance, des facteurs qui l'ont portée et
des risques qui pourraient la faire tomber.

Parcours utilisateur : **sport → compétition → équipe 1 → équipe 2 → bouton**.
Tout le reste est invisible.

---

## 1. Installation

### Prérequis
- **Python 3.11 ou plus** — [python.org/downloads](https://www.python.org/downloads/)
  (sur Windows, cochez **« Add python.exe to PATH »**).

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

(macOS / Linux : `source .venv/bin/activate`)

```bash
python -m pip install -r requirements.txt
```

```bash
copy .env.example .env
```

(macOS / Linux : `cp .env.example .env`)

```bash
streamlit run app.py
```

L'interface s'ouvre sur `http://localhost:8501`.

> **Sans aucune clé API**, l'agent fonctionne : effectifs complets, résultats
> récents, classements, confrontations directes et alertes de blessure viennent
> de sources publiques ouvertes. Ce qui manque, ce sont **les cotes** — la clé
> The Odds API est celle qui change le plus la qualité des décisions.

---

## 2. L'agent de décision

### Neuf modules, une responsabilité chacun

```
     ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
     │  COLLECTE    │──▶│  VALIDATION  │──▶│    FUSION    │
     │ ~10 sources  │   │ écarte ce qui│   │ dédoublonne, │
     │ en parallèle │   │ n'est pas sûr│   │ arbitre      │
     └──────────────┘   └──────────────┘   └──────┬───────┘
                                                   ▼
     ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
     │  SIMULATION  │◀──│   MARCHÉ     │◀──│ STATISTIQUES │
     │ 20 000 matchs│   │ consensus,   │   │ forces,      │
     │ rejoués      │   │ dérive       │   │ pondérations │
     └──────┬───────┘   └──────────────┘   └──────────────┘
            ▼
     ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
     │CONTRADICTIONS│──▶│  SCÉNARIOS   │──▶│AUTO-ÉVALUATION│
     │ signaux      │   │ alternatifs  │   │ 5 critères   │
     │ opposés      │   │ et risques   │   │ mesurés      │
     └──────────────┘   └──────────────┘   └──────┬───────┘
                                                   ▼
                                        ┌────────────────────┐
                                        │  DÉCISION FINALE   │
                                        │ pronostic + proba  │
                                        │ + confiance +      │
                                        │ facteurs + risques │
                                        └────────────────────┘
```

Chaque étape est isolée : **si l'une échoue, l'agent continue** avec ce qu'il a
et abaisse sa confiance. Il ne s'arrête jamais sur une source manquante.

| Module | Fichier | Rôle |
|---|---|---|
| Collecte & fusion | `research.py` | interroge toutes les sources en parallèle, recoupe |
| Validation | `agent/validation.py` | écarte les données aberrantes, incohérentes ou périmées |
| Raisonnement | `agent/factors.py` | transforme les données en critères pondérés |
| Marché | `agent/market.py` | consensus, marge, dispersion, dérive des cotes |
| Simulation | `engine.py` | Poisson/Dixon-Coles, Monte Carlo, marchés dérivés |
| Contradictions | `agent/contradictions.py` | cherche activement les signaux opposés |
| Scénarios | `agent/scenarios.py` | déroulements alternatifs, mesurés sur les simulations |
| Auto-évaluation | `agent/introspection.py` | note la solidité de sa propre analyse |
| Décision | `agent/decision.py` | assemble la recommandation finale |
| Mémoire | `agent/memory.py` | journal, calibration, propositions de réglage |
| Orchestration | `agent/pipeline.py` | enchaîne le tout, tolérant aux pannes |

### Raisonnement multicritère

Douze critères, chacun signé, pondéré et assorti d'un niveau de confiance qui
dépend de la taille de l'échantillon. Les poids vivent dans `.env` et sont
faits pour être ajustés.

| Critère | Poids | Déjà dans la simulation ? |
|---|---|---|
| Forme récente | 1.00 | ✅ |
| Consensus des bookmakers | 1.20 | ✅ |
| Efficacité offensive (xG si disponibles) | 0.85 | ✅ |
| Solidité défensive | 0.85 | ✅ |
| Avantage du terrain | 0.70 | ✅ |
| Position au classement | 0.60 | ✅ |
| Confrontations directes | 0.45 | ✅ |
| Temps de récupération | 0.35 | ✅ |
| **Évolution des cotes** | 0.55 | ❌ ajuste la décision |
| **Dynamique (séries)** | 0.40 | ❌ ajuste la décision |
| **Charge de calendrier** | 0.30 | ❌ ajuste la décision |
| **Enjeu du match** | 0.25 | ❌ ajuste la décision |

**Pourquoi cette colonne compte.** Un critère déjà consommé par la simulation
(la forme nourrit les forces d'équipe) est affiché pour *expliquer* la
décision, mais **n'est pas réappliqué** : le compter deux fois gonflerait
artificiellement la probabilité. Seuls les quatre critères du bas ajustent
réellement le résultat, dans une limite de ±8 % (`AGENT_TILT_STRENGTH`).

Un critère dont la donnée manque est marqué indisponible. Il n'est **ni
inventé, ni remplacé par une valeur neutre arbitraire**.

### Détection des contradictions

L'agent cherche activement les désaccords, parce qu'un désaccord est une
information :

- nos statistiques et les bookmakers ne désignent pas le même favori ;
- une équipe est notre favorite mais **sa cote s'allonge** ;
- excellente forme d'un côté, **absence signalée** de l'autre ;
- l'historique des confrontations **contredit** la forme du moment ;
- les critères solides se **partagent** entre les deux équipes ;
- les bookmakers ne s'accordent pas entre eux ;
- les sources ne rapportent pas les mêmes résultats.

Chaque contradiction porte une gravité et **fait baisser la confiance**
(jusqu'à −2,5 points sur 10).

### Scénarios alternatifs

L'agent ne s'arrête pas au déroulement le plus probable. Il décompose le risque
d'échec en scénarios nommés — nul, surprise de l'outsider, match verrouillé,
victoire d'un but, prolongation… — **tous mesurés sur les mêmes simulations**.
Ce sont des probabilités réelles, jamais des hypothèses ajoutées à la main.

### Auto-évaluation

Après chaque analyse, l'agent se note sur cinq critères observables :

| Critère | Comment il est mesuré |
|---|---|
| Qualité des données | fiabilité moyenne et maximale des sources retenues |
| Quantité de données | cotes, profondeur d'historique, classement, H2H, xG |
| Fraîcheur | âge de la donnée la plus ancienne du dossier |
| Cohérence des sources | gravité cumulée des contradictions |
| **Stabilité des probabilités** | les tirages sont découpés en 5 lots indépendants et on mesure la dispersion du résultat |

Cette note module la confiance affichée, sans jamais toucher aux probabilités.

### Décision finale

```
Notre pronostic       Victoire Arsenal
Chances de réussite   67 %
Notre confiance       8,4 / 10
Facteurs clés         forme récente · consensus des bookmakers · classement
Risques               nul (22 %) · surprise (18 %) · victoire d'un but (34 %)
```

Un marché n'est recommandé que si sa probabilité tombe dans une fourchette
**exploitable** (45–80 %). Au-delà de 80 %, un pronostic est quasi certain donc
sans valeur : « plus de 0,5 but » à 97 % se paierait 1,03. Le nul sec n'est
jamais recommandé. **Si rien n'est défendable, l'agent s'abstient** et le dit.

### Reproductibilité

La graine aléatoire dérive d'une **empreinte des données d'entrée**
(`AGENT_DETERMINISTIC=true`). Deux analyses du même match sur les mêmes données
produisent exactement la même recommandation, la même probabilité et la même
empreinte — affichée dans la sortie technique pour pouvoir être comparée.

---

## 3. Mémoire et apprentissage encadré

Chaque analyse est journalisée localement (`.cache/ledger.json`) :
recommandation, probabilité, confiance, empreinte. Quand le résultat réel
devient connu, l'agent le confronte à son annonce.

Il en tire :

- un **taux de réussite** par famille de marché ;
- un **score de Brier** (qualité globale des probabilités) ;
- une **courbe de calibration** : « quand j'annonce 70 %, ça se réalise
  combien de fois ? ».

> Rien n'est affiché tant qu'il y a moins de **20 résultats connus** : en
> dessous, aucune conclusion n'est honnête.

### La règle stricte sur les réglages

Quand la calibration montre un biais net, l'agent **propose** un ajustement —
par exemple donner plus de poids au marché s'il s'avère trop optimiste. La
proposition apparaît dans l'interface avec son motif et sa preuve chiffrée,
et deux boutons : **Accepter** / **Refuser**.

**Accepter n'applique rien.** La valeur est écrite dans
`.cache/parameter_overrides.json`, à vous de la reporter dans `.env` si vous le
souhaitez. Le moteur en cours d'exécution n'est jamais modifié à votre insu.
Un test dédié vérifie cette garantie.

---

## 4. Prêt pour un modèle d'IA

Quatre interfaces sont définies dans `agent/contracts.py`. Chacune a
aujourd'hui une implémentation déterministe ; la remplacer suffit à changer le
comportement de l'agent, **sans toucher au reste de l'application** :

```python
from agent import AnalysisAgent

agent = AnalysisAgent(
    hub,
    score_model=MonModeleDeScores(),      # prédiction des scores
    weighting_policy=MesPoidsAppris(),    # pondération des facteurs
    pattern_detector=MonDetecteur(),      # motifs complexes
    narrator=MonRedacteur(),              # synthèse en langage naturel
)
```

Un composant injecté qui échoue est neutralisé automatiquement : l'agent
retombe sur son implémentation interne et poursuit l'analyse. Deux tests le
vérifient.

**Contrainte non négociable pour tout remplaçant** : ne jamais introduire un
fait absent des données.

---

## 5. Compétitions et effectifs

Tout est déclaré dans **`config.py` → `SUPPORTED_COMPETITIONS`**.

| Sport | Compétitions actives |
|---|---|
| ⚽ Football | Premier League, La Liga, Bundesliga, Serie A, Ligue 1, Champions League, Europa League, Conference League, Supercoupe d'Europe |
| 🏀 Basket | NBA, WNBA |
| 🏒 Hockey | NHL |
| 🎾 Tennis | ATP/WTA Grand Chelem, ATP Tour, WTA Tour, ATP/WTA Finals, Coupe Davis, Billie Jean King Cup |

Effectifs obtenus **sans aucune clé API**, construits à partir des données et
jamais codés en dur :

| Premier League | La Liga | Bundesliga | Ligue 1 | NBA | WNBA | NHL |
|---|---|---|---|---|---|---|
| 20/20 | 20/20 | 18/18 | 18/18 | 30/30 | 15/15 | 32/32 |

**Une seule saison à la fois.** À l'intersaison, les sources ne basculent pas
en même temps : l'encyclopédie annonce déjà la saison à venir quand les
calendriers officiels en sont encore à la précédente. L'agent retient la saison
la plus récente et **écarte les sources restées sur l'ancienne** — sinon promus
et relégués se mélangeraient.

---

## 6. Sources de données

Toutes sont des **API officielles, des services publics ou des jeux de données
librement réutilisables**. Aucun scraping, aucun contournement de conditions
d'utilisation.

| Source | Apporte | Clé | Fiabilité |
|---|---|---|---|
| API publique NHL | effectifs, classements, calendriers, **confrontations** | non | 0.96 |
| The Odds API | cotes de plusieurs bookmakers, joueurs de tennis | oui | 0.95 |
| API-Football | xG, corners, cartons, tirs, possession, classements, H2H | oui | 0.92 |
| football-data.org | résultats et classements | oui | 0.90 |
| balldontlie | NBA | oui | 0.88 |
| **football-data.co.uk** | corners, tirs, cartons + **cotes de clôture** | non | 0.87 |
| openfootball | calendriers complets + **confrontations sur 5 saisons** | non | 0.85 |
| Wikipédia (MediaWiki) | effectif exact de la saison | non | 0.80 |
| Wikidata (SPARQL) | franchises des ligues fermées | non | 0.75 |
| Open-Meteo | météo au coup d'envoi | non | 0.70 |
| TheSportsDB | repli universel | non | 0.55 |
| Google News (RSS) | alerte blessure/absence | non | 0.40 |

### Profondeur des données, sans aucune clé

Les paliers gratuits des API bridées plafonnent à **un seul match**
d'historique, ce qui ne permet aucune estimation sérieuse : toutes les équipes
finissaient par se ressembler. Les résultats viennent donc en priorité des
**calendriers openfootball**, qui contiennent la saison entière.

| | avant | maintenant |
|---|---|---|
| Historique par équipe | 1 match | **10 matchs** |
| Moyenne de buts de la compétition | constante 1,40 | **mesurée sur 380 matchs** |
| Confrontations directes | absentes | **5 saisons** |

### Les cotes : ce qui marche sans clé, et ce qui n'y arrive pas

**Les cotes d'un match à venir exigent une clé.** Aucune source légitime ne les
publie librement ; sans `ODDS_API_KEY`, elles resteront indisponibles quel que
soit le code. L'inscription est gratuite sur
[the-odds-api.com](https://the-odds-api.com/) (500 requêtes/mois).

**Ce qui fonctionne malgré tout, sans aucune clé :** les archives
football-data.co.uk publient les **cotes de clôture** de tous les matchs joués.
Le moteur en tire, pour chaque équipe, ce que le marché lui accordait à
domicile et à l'extérieur, puis combine les deux profils par moyenne
géométrique. Cela donne un **repère de marché** qui ancre le modèle.

Ce repère est traité pour ce qu'il est, jamais davantage :

| | Cote du match | Repère de saison |
|---|---|---|
| Poids dans la calibration | 60 % | **33 %** |
| Crédit de confiance | 3,0 / 3 | **1,3 / 3** |
| Détection d'opportunité | oui | **jamais** (aucun prix à comparer) |
| Affichage | « cotes des bookmakers » | « repère de saison », explicitement distingué |

Effet mesuré sur la Premier League : confiance passée de **5,5 à 7,6–8,1** pour
les équipes couvertes, et restée à **2,1** pour un promu sans historique dans
la division — la distinction est conservée.

### Pourquoi les cotes manquent parfois

Chaque tentative est tracée, et la cause exacte est affichée plutôt qu'un
« indisponible » muet :

| Cause | Ce que dit l'application |
|---|---|
| Pas de clé configurée | « Aucune clé de cotes configurée » + comment l'ajouter |
| Compétition non couverte | « Compétition introuvable chez le fournisseur » |
| Match hors calendrier | « Rencontre absente du calendrier », avec la meilleure concordance trouvée |
| Quota épuisé | « Quota de requêtes épuisé » |
| Aucun marché publié | « Aucun marché exploitable pour ce match » |

Le moteur essaie plusieurs **régions de bookmakers** (`eu`, `uk`, `us`) en
repli l'une de l'autre, et demande les marchés 1X2, totaux, handicaps et
« les deux marquent ». Il ne conclut à l'indisponibilité qu'après avoir
épuisé toutes les sources configurées.

### Historique des confrontations

Il est **toujours recherché**, et disponible sans aucune clé :

- **Football** — les calendriers openfootball sont parcourus sur **5 saisons**
  pour retrouver toutes les rencontres entre les deux clubs, avec leurs scores.
- **Hockey** — les calendriers NHL des trois dernières saisons sont relus
  (sans appel réseau supplémentaire : ils sont déjà téléchargés pour la forme).
- **Avec clé** — API-Football fournit l'historique directement.

Quand aucune rencontre commune n'existe (première confrontation, promu),
l'interface l'indique clairement plutôt que d'afficher un vide.

### Ce qui n'entre PAS dans le calcul

La **météo** et les **blessures** sont affichées comme contexte et pèsent sur
la ligne de risque, mais ne modifient jamais les probabilités : aucun
coefficient honnêtement calibré n'existe pour elles. Fabriquer un « pluie =
−0,2 but » serait exactement l'invention que ce projet refuse.

---

## 7. Mise en ligne

### Streamlit Community Cloud

Gratuit, sans carte bancaire. **Le dépôt local est déjà initialisé et commité**
(`.env` et `.streamlit/secrets.toml` exclus, vérifié fichier par fichier).

**Le plus simple : double-cliquez sur `PUBLIER-PRONOSTAT.bat`.** Il répare le
dépôt, remet les commits à votre nom, ouvre le formulaire GitHub prérempli,
envoie le code et ouvre Streamlit Cloud. Aucun jeton à créer : l'authentification
se fait par navigateur.

Le détail, si vous préférez la main :

**1. Publier sur GitHub.** Créez un dépôt vide sur
[github.com/new](https://github.com/new) — nommez-le `pronostat`, sans README
ni .gitignore — puis :

```bash
git remote add origin https://github.com/VOTRE-COMPTE/pronostat.git
git push -u origin main
```

À l'invite de connexion, choisissez **« Sign in with your browser »** :
Git Credential Manager, livré avec Git pour Windows, gère l'autorisation. Un
jeton d'accès personnel reste possible mais n'est plus nécessaire.

**2. Déployer.** Sur [share.streamlit.io](https://share.streamlit.io),
connectez-vous avec GitHub → **Create app** → dépôt `pronostat`, branche `main`,
fichier `app.py`.

> ⚠️ **Ouvrez « Advanced settings » et choisissez Python 3.12 AVANT de cliquer
> sur Deploy.** Le fichier `runtime.txt` est fréquemment ignoré par Streamlit
> Community Cloud, et la version de Python **ne peut plus être changée après
> coup** : il faudrait supprimer l'application et la redéployer.

Vous obtenez une URL en `.streamlit.app`, utilisable depuis n'importe quel
appareil.

**3. Ajouter les clés.** **Settings → Secrets** : collez le contenu de
`.streamlit/secrets.toml.example` complété, plus
`PRONOSTAT_ENV = "production"`. Aucun redéploiement nécessaire. Le pas-à-pas
complet est dans `APRES-DEPLOIEMENT.md`.

> `requirements.txt` ne contient que le nécessaire à l'exécution ;
> `requirements-dev.txt` ajoute `pytest` pour le développement local. Le
> démarrage en ligne n'en est que plus rapide.

### Développement et production

Le même code tourne dans les deux cas ; seul `PRONOSTAT_ENV` change.

| | développement | production |
|---|---|---|
| Journalisation | `INFO` (détaillée) | `WARNING` (silencieuse) |
| Rouages internes | visibles | masqués |
| Cache | dossier du projet | bascule automatique en dossier temporaire si le disque est en lecture seule |

Les réglages sont lus **d'abord dans les variables d'environnement, puis dans
les secrets Streamlit** (`config._secret`) : rien à modifier pour passer de
l'un à l'autre.

### Mettre à jour

```bash
git push
```

Streamlit Community Cloud redéploie automatiquement. Pour changer une clé,
passez par **Settings → Secrets** : aucun redéploiement n'est nécessaire.

### Autre voie : Firebase Hosting + Cloud Run

Le projet est préconfiguré pour **pronostat-8a2a8** (`.firebaserc`).

> **Ce qu'il faut comprendre d'abord.** Firebase Hosting sert des fichiers
> statiques : il ne peut pas exécuter Python. Le SDK JavaScript de Firebase
> (`initializeApp`, `getAnalytics`) s'adresse à une page web classique, pas à
> une application Streamlit. La combinaison qui fonctionne est donc
> **Cloud Run pour l'application + Firebase Hosting en façade** — c'est ce que
> configure le `firebase.json` fourni, et cela donne bien une URL
> `pronostat-8a2a8.web.app`.

> ⚠️ **Cloud Run exige le plan Blaze** (paiement à l'usage, carte bancaire
> requise). Le plan gratuit Spark ne permet ni Cloud Run ni les appels réseau
> sortants. Blaze inclut un quota gratuit mensuel largement suffisant pour un
> usage personnel, mais la carte est obligatoire. **Si vous voulez éviter
> cela, Streamlit Community Cloud reste entièrement gratuit et sans carte.**

Déploiement, une fois le plan Blaze activé :

```bash
gcloud run deploy pronostat --source . --region europe-west1 --allow-unauthenticated --project pronostat-8a2a8
```

Puis Firebase Hosting devant :

```bash
firebase deploy --only hosting
```

Les clés se déclarent comme variables d'environnement du service — **jamais
dans le code** :

```bash
gcloud run services update pronostat --region europe-west1 --project pronostat-8a2a8 --update-env-vars ODDS_API_KEY=votre_cle,PRONOSTAT_ENV=production
```

Mises à jour ultérieures : relancer la commande `gcloud run deploy`. Le
`Dockerfile` met les dépendances dans une couche séparée, donc un
redéploiement sans changement de `requirements.txt` est rapide.

**La clé d'API web Firebase n'est pas nécessaire ici.** Elle sert au SDK
JavaScript côté navigateur ; le déploiement n'utilise que l'identifiant de
projet. Ces clés web sont d'ailleurs publiques par conception (elles
identifient le projet, elles ne l'autorisent pas) — la vraie protection passe
par les règles de sécurité Firebase et par la restriction de la clé dans la
console Google Cloud.

Le même conteneur fonctionne sur Render, Fly.io ou Azure Container Apps : ils
fournissent tous la variable `PORT` que le `Dockerfile` respecte.

### Sur téléphone et tablette

Une fois déployée, l'application s'ouvre dans **n'importe quel navigateur** —
Android, iPhone, iPad, ordinateur — via la même URL. Il n'y a pas d'application
à installer : la mise en page est déjà adaptée aux petits écrans (les cartes
passent en colonne sous 640 px).

Pour un accès en un geste, ajoutez la page à l'écran d'accueil :
- **iPhone / iPad** — Safari → Partager → « Sur l'écran d'accueil ».
- **Android** — Chrome → menu → « Ajouter à l'écran d'accueil ».

L'icône se comporte alors comme une application.

> Une simulation demande quelques secondes de réseau : sur mobile en 4G, la
> première analyse d'une compétition est la plus lente (elle remplit le cache),
> les suivantes sont quasi instantanées.

### Mode diagnostic

`PRONOSTAT_DEBUG=true` ajoute un panneau détaillant les sources interrogées,
les données écartées et les valeurs du modèle. Il est **désactivé de force**
quand `PRONOSTAT_ENV=production` : les utilisateurs finaux ne le voient jamais,
même si la variable traîne dans la configuration.

### Points d'attention en ligne

- **Renseignez `PRONOSTAT_USER_AGENT`** avec un contact valide : MediaWiki et
  Wikidata l'exigent pour un usage automatisé.
- Le disque est éphémère : le cache et le journal repartent à zéro à chaque
  redémarrage. Pour conserver l'historique de calibration sur la durée,
  montez un stockage persistant ou exportez `.cache/ledger.json`.
- Les quotas gratuits sont partagés par **tous** les visiteurs. Surveillez le
  bandeau de quota.

### Migrer plus tard vers une API

L'agent ne dépend pas de Streamlit : `config`, `data_sources`, `research`,
`engine` et `agent` n'importent jamais l'interface. Une API HTTP se branche
directement sur la sortie déjà sérialisable :

```python
from fastapi import FastAPI
from agent import AnalysisAgent
from data_sources import DataHub
import config as cfg

api = FastAPI()
agent = AnalysisAgent(DataHub())

@api.get("/analyse/{sport}/{competition}/{home}/{away}")
def analyse(sport: str, competition: str, home: str, away: str):
    comp = cfg.competition(sport, competition)
    return agent.analyse_match(comp, home, away).as_payload()
```

`as_payload()` renvoie déjà tout le nécessaire : décision, probabilités,
facteurs, risques, contradictions, auto-évaluation, données écartées.

---

## 8. Structure du projet

```
app.py              interface (sport → compétition → équipes → bouton)
agent/              l'agent de décision
  contracts.py        types partagés + interfaces remplaçables par une IA
  validation.py       contrôle de vraisemblance des données
  factors.py          raisonnement multicritère
  market.py           lecture du marché des cotes
  contradictions.py   détection des signaux opposés
  scenarios.py        déroulements alternatifs
  introspection.py    auto-évaluation
  decision.py         décision finale
  memory.py           journal, calibration, propositions de réglage
  pipeline.py         orchestration tolérante aux pannes
research.py         collecte multi-sources et fusion
data_sources.py     clients des sources, cache, quotas
engine.py           probabilités, simulations, marchés dérivés
config.py           compétitions, pondérations, environnement
tests/              263 tests unitaires
```

### Tests

```bash
python -m pytest -q
```

Couvrent notamment : le registre de compétitions, la fusion multi-sources
(isolation des saisons, dédoublonnage, arbitrage), la validation (scores
aberrants, cotes incohérentes, historique périmé), le raisonnement
multicritère (bornes, confiance selon l'échantillon, **non-double-comptage**),
la détection des contradictions, les scénarios issus des simulations,
l'auto-évaluation, la décision (abstention, bornes de l'ajustement), la
**robustesse totale** (toutes les sources tombent → l'agent produit quand même
un résultat), la **reproductibilité**, les **points d'insertion d'un modèle
d'IA** (injection, repli en cas de panne), et la garantie que
**l'acceptation d'un réglage ne modifie jamais la configuration en cours**.

---

## 9. Ajouter une compétition

Une seule entrée dans `config.py`, aucune modification du moteur :

```python
Competition(
    key="liga_portugal", label="Liga Portugal", sport="football",
    odds_key="soccer_portugal_primeira_liga",
    odds_title="Primeira Liga - Portugal",
    sportsdb_league="Portuguese Primeira Liga",
    football_data_code="PPL", api_football_id=94,
    openfootball_code="pt.1",          # calendriers + confrontations
    wikipedia_page="Primeira Liga",    # effectif de la saison
    expected_teams=18,
),
```

Prêtes à brancher (openfootball les couvre déjà) : Eredivisie `nl.1`, Jupiler
Pro League `be.1`, Süper Lig `tr.1`, Championship `en.2`, Bundesliga 2 `de.2`.

## 10. Ajouter une source

```python
class MaSource(BaseProvider):
    name = "ma_source"
    label = "Statistiques avancées"     # nom affiché, en langage courant

    def handles(self, comp):
        return self.enabled and comp.api_football_id is not None

    def form(self, comp, team):
        res = self.http.get_json(
            "https://…", ttl=cfg.TTL.form, provider=self.name,
            scope=comp.scope,           # cache + quota automatiques
        )
        if not res:
            return None                 # → l'agent passe à la suivante
        payload, ts, from_cache = res
        return TeamForm(team, comp.sport, [...], self._prov(ts, from_cache, "…"))
```

1. Insérez-la dans `DataHub.providers`.
2. Déclarez sa fiabilité dans `config.SOURCE_RELIABILITY` et son nom public
   dans `SOURCE_PUBLIC_NAMES`.
3. Ajoutez sa clé, son interrupteur, et une `QuotaRule` si elle est limitée.

L'agent l'intègre automatiquement à la fusion, la recoupe avec les autres et la
compte dans son auto-évaluation.

---

## 11. Posture

- Les probabilités viennent de **cotes réelles** et de simulations
  **réellement exécutées**. Elles reflètent le consensus et les données
  disponibles — **jamais une certitude**.
- L'agent dit clairement quand une donnée manque, quand ses signaux se
  contredisent, et quand il préfère s'abstenir.
- Le sport reste incertain : blessures, arbitrage, prolongations. L'application
  donne un **avantage de méthode, pas une garantie**.
- Vérifiez les compositions officielles avant de parier.
- **Aucun résultat garanti — jouez responsable. 18+**
