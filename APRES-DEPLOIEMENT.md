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

THESPORTSDB_API_KEY = "3"
ODDS_REGIONS = "eu,uk,us"
MARKET_WEIGHT = "0.60"
MC_SIMS = "20000"
```

Pourquoi ces valeurs :

| Réglage | Raison |
|---|---|
| `PRONOSTAT_ENV = "production"` | Désactive de force le panneau de diagnostic |
| `PRONOSTAT_CACHE_DIR = "/tmp/…"` | Le disque de Streamlit Cloud est éphémère : le cache doit aller dans un dossier temporaire |
| `PRONOSTAT_USER_AGENT` | Wikipédia et Wikidata exigent un contact valide, sinon ils bloquent les requêtes |
| `THESPORTSDB_API_KEY = "3"` | Clé de test publique gratuite — repli universel |

L'application redémarre seule après la sauvegarde.

---

## 2. Ajouter la clé des cotes (optionnel, recommandé)

Sans elle, l'application **fonctionne**, mais s'appuie sur le repère de saison au lieu des cotes réelles du match — et le tennis reste indisponible, car les tournois n'existent que via cette source.

1. Inscription gratuite : <https://the-odds-api.com/> (≈ 500 requêtes/mois)
2. Copier la clé reçue
3. **Settings → Secrets**, ajouter la ligne :

```toml
ODDS_API_KEY = "votre_cle_ici"
```

Autres clés facultatives, même principe : `RAPIDAPI_KEY` (API-Football : xG, corners, tirs), `FOOTBALL_DATA_API_KEY` (classements grandes ligues), `BALLDONTLIE_API_KEY` (NBA). La liste complète et commentée est dans `.env.example`.

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

`.env`, `.streamlit/secrets.toml`, `.venv/`, les caches — tous exclus par `.gitignore`. Vérifié fichier par fichier : aucune clé ne figure dans les 39 fichiers envoyés sur GitHub.

**Le dépôt étant public**, votre adresse e-mail apparaît dans l'historique des commits (c'est le fonctionnement normal de git, et c'est ce qui rattache les commits à votre profil GitHub). Pour la masquer : GitHub → Settings → Emails → cocher *Keep my email addresses private*, puis utiliser l'adresse `…@users.noreply.github.com` qui y est indiquée.
