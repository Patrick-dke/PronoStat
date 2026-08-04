# Après le déploiement — 2 réglages

À faire une fois que l'application tourne sur `https://…streamlit.app`.

---

## 0. Pendant le déploiement — le seul réglage irréversible

Au moment de créer l'application sur Streamlit Cloud, **ouvrez « Advanced settings » et choisissez Python 3.12 avant de cliquer sur Deploy.**

Deux raisons :

- Le fichier `runtime.txt` du projet est **fréquemment ignoré** par Streamlit Community Cloud, qui impose alors sa version par défaut, plus récente ([bug ouvert](https://github.com/streamlit/streamlit/issues/15326))
- La version de Python **ne peut plus être modifiée ensuite** : il faut supprimer l'application et la redéployer ([documentation Streamlit](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/upgrade-python))

Si l'installation échoue avec des erreurs de compilation autour de `numpy` ou `pandas`, c'est presque toujours ça : supprimez l'application et recréez-la en 3.12.

---

## 1. Coller les secrets

Dans votre application en ligne : **Settings → Secrets**, coller ce bloc tel quel, puis **Save**.

```toml
PRONOSTAT_ENV = "production"
PRONOSTAT_LOG_LEVEL = "WARNING"
PRONOSTAT_DEBUG = "false"
PRONOSTAT_CACHE_DIR = "/tmp/pronostat-cache"
PRONOSTAT_USER_AGENT = "PronoStat/3.0 (contact: manjiadiandoukepatricksylvain@gmail.com)"

ODDS_API_KEY = "COLLEZ_VOTRE_CLE_ICI"

THESPORTSDB_API_KEY = "3"
ODDS_REGIONS = "eu,uk,us"
MARKET_WEIGHT = "0.60"
MC_SIMS = "20000"
```

> Remplacez `COLLEZ_VOTRE_CLE_ICI` par la clé reçue à l'inscription sur
> <https://the-odds-api.com/>. **C'est la ligne qui fait apparaître les cotes** :
> sans elle, l'application affiche en permanence « Aucune clé de cotes
> configurée » et le tennis reste vide. Si vous n'avez pas encore la clé,
> supprimez la ligne entière — vous la rajouterez plus tard.

Pourquoi ces valeurs :

| Réglage | Raison |
|---|---|
| `PRONOSTAT_ENV = "production"` | Désactive de force le panneau de diagnostic |
| `PRONOSTAT_CACHE_DIR = "/tmp/…"` | Le disque de Streamlit Cloud est éphémère : le cache doit aller dans un dossier temporaire |
| `PRONOSTAT_USER_AGENT` | Wikipédia et Wikidata exigent un contact valide, sinon ils bloquent les requêtes |
| `THESPORTSDB_API_KEY = "3"` | Clé de test publique gratuite — repli universel |

L'application redémarre seule après la sauvegarde.

---

## 2. Obtenir la clé des cotes

C'est la clé qui débloque le cœur de la méthode : sans elle, les probabilités ne sont plus ancrées aux cotes réelles du marché, et l'application affiche « Aucune clé de cotes configurée ».

1. Inscription gratuite : <https://the-odds-api.com/> (≈ 500 requêtes/mois, sans carte bancaire)
2. La clé arrive par e-mail, immédiatement
3. La coller dans le bloc du §1, à la place de `COLLEZ_VOTRE_CLE_ICI`

**En local aussi**, pour tester sur votre ordinateur : ouvrez `.env` dans le Bloc-notes, ligne 10, et complétez :

```
ODDS_API_KEY=votre_cle_ici
```

Sans espaces autour du `=`, sans guillemets — le format `.env` diffère de celui des secrets Streamlit.

### Ce que la clé change, sport par sport

| Sport | Sans clé | Avec clé |
|---|---|---|
| Football | Modèle statistique seul, repère de saison | Cotes réelles + no-vig, confiance nettement plus haute |
| Basket | Idem, via balldontlie si sa clé est posée | Cotes réelles, totaux et spreads |
| Hockey | Idem, via l'API NHL publique | Cotes réelles, puck line |
| **Tennis** | **Indisponible** — aucune liste de joueurs | Tournois et joueurs en cours |

Le tennis est le cas dur : les tournois n'existent que via cette source, il n'y a pas de repli gratuit.

### Autres clés, facultatives

Même principe, chacune ajoute des données sans être nécessaire : `RAPIDAPI_KEY` (API-Football : xG, corners, tirs), `FOOTBALL_DATA_API_KEY` (classements grandes ligues), `BALLDONTLIE_API_KEY` (NBA). La liste commentée est dans `.env.example`.

---

## Mettre à jour l'application plus tard

Toute modification du code se publie ainsi, depuis le dossier du projet :

```
git add -A
git commit -m "description de la modification"
git push
```

Streamlit Cloud redéploie automatiquement en une minute.

---

## Une limite à connaître : l'historique n'est pas permanent

L'onglet « Mes analyses précédentes » et les statistiques de calibration sont stockés dans des fichiers JSON (`ledger.json`, `history.json`) posés sur le disque du serveur.

Sur Streamlit Community Cloud, **ce disque est effacé à chaque redémarrage** de l'application — redéploiement, mise en veille après quelques jours sans visite, ou dépassement de ressources. Vos analyses passées disparaissent alors, et la calibration repart de zéro.

Ce n'est pas un défaut du code : c'est la nature de l'hébergement gratuit. Les trois issues, par effort croissant :

| Option | Ce que ça implique |
|---|---|
| Accepter | Gratuit. L'historique sert sur quelques jours, pas sur la durée |
| Base externe (Supabase, Firebase) | Gratuit aussi, mais demande un compte et une modification de `agent/memory.py` |
| Bouton d'export / import | À développer : télécharger le `ledger.json` et le réinjecter après un redémarrage |

Dites-le moi si vous voulez que je m'occupe de l'une des deux dernières.

---

## Ce qui n'est jamais publié

`.env`, `.streamlit/secrets.toml`, `.venv/`, les caches — tous exclus par `.gitignore`. Vérifié fichier par fichier : aucune clé ne figure dans les 41 fichiers envoyés sur GitHub.

**Le dépôt est privé** : personne d'autre que vous ne voit le code, et votre adresse e-mail dans l'historique des commits n'est visible que de vous. Rien à masquer.

---

## Dépôt privé : les trois conséquences

**1. Une autorisation GitHub supplémentaire est obligatoire.** Les permissions par défaut de Streamlit ne couvrent que les dépôts publics : sans cette étape, `pronostat` n'apparaîtra tout simplement pas dans la liste au moment de créer l'application.

Votre nom en haut à droite → **Settings** → **Linked accounts** → sous *Source control*, **Authorize**. Streamlit crée alors une clé de déploiement en lecture seule ; GitHub vous notifie par e-mail, c'est le fonctionnement normal.

**2. L'application est privée elle aussi.** Elle hérite de la confidentialité du dépôt. Concrètement, sur le téléphone il faudra se connecter avec la même adresse e-mail que votre compte Streamlit — sinon la page affiche un refus d'accès.

Deux réglages depuis le bouton **Share** de l'application :

| Vous voulez | À faire |
|---|---|
| Garder pour vous | Ne rien changer |
| Inviter quelques personnes | *Share* → ajouter leurs adresses e-mail |
| Ouvrir à tous | *Share* → passer l'application en **Public** |

Le dépôt reste privé dans les trois cas : rendre l'application publique n'expose pas le code.

Garder l'application privée a un intérêt concret au-delà de la confidentialité : votre quota gratuit de cotes (≈ 500 requêtes/mois) ne peut pas être consommé par des inconnus.

**3. Une seule application privée à la fois.** C'est la limite du plan gratuit. Pour en déployer une seconde, il faudrait rendre celle-ci publique ou la supprimer.
