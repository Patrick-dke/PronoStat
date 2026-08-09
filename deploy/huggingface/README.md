---
title: PronoStat API
emoji: 📊
colorFrom: gray
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

# PronoStat — API

Couture HTTP entre l'orchestration n8n et le moteur d'analyse PronoStat.
Ce service n'implémente aucune logique d'analyse : il appelle les mêmes
modules que l'interface Streamlit.

## Routes

| Route | Authentification | Rôle |
|---|---|---|
| `GET /health` | non | état du service |
| `GET /quota` | oui | crédits restants |
| `POST /analysis` | oui | lance une analyse |
| `GET /analysis/pending` | oui | analyses en attente de résultat |
| `GET /analysis/{id}` | oui | relit une analyse archivée |
| `GET /score` | oui | score final d'une rencontre |
| `POST /result` | oui | enregistre le score réel |

L'authentification se fait par en-tête `Authorization: Bearer <jeton>`.

**Sans `PRONOSTAT_API_TOKEN` configuré, le service reste fermé** et répond
503 sur toutes les routes protégées. Ce choix est délibéré : cette API
dépense un quota d'API payant, un oubli de configuration doit se voir
immédiatement plutôt que laisser le service ouvert à tous.

## Variables à définir

Dans **Settings → Variables and secrets** du Space.

| Secret | Rôle |
|---|---|
| `PRONOSTAT_API_TOKEN` | jeton d'accès — inventez une chaîne longue |
| `ODDS_API_KEY` | cotes des bookmakers |
| `FIREBASE_SERVICE_ACCOUNT` | journal permanent (facultatif) |
| `ODDS_QUOTA_RESET_DAY` | jour de remise à zéro du quota (facultatif) |

## Limites du palier gratuit

Le Space s'endort après une période d'inactivité prolongée ; le premier
appel qui suit prend quelques dizaines de secondes. Prévoyez un délai
d'attente d'au moins 60 secondes côté n8n.

Seul `/tmp` est inscriptible. Le journal des pronostics y est donc effacé à
chaque redémarrage, sauf si `FIREBASE_SERVICE_ACCOUNT` est renseigné.

---

Code source : <https://github.com/Patrick-dke/PronoStat>
