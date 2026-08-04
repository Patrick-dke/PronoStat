# Déployer PronoStat sur Firebase / Google Cloud

Ce document existe parce que le montage « évident » ne fonctionne pas, et qu'il
échoue **silencieusement**. Lisez le premier paragraphe avant tout le reste.

---

## Le piège : Firebase Hosting ne peut pas servir Streamlit

Firebase Hosting sait rediriger le trafic vers un conteneur Cloud Run, via une
règle `rewrite`. C'était la configuration initiale de ce projet :

```json
"rewrites": [{ "source": "**", "run": { "serviceId": "pronostat" } }]
```

Elle se déploie sans le moindre message d'erreur. La page se charge. Le logo
s'affiche. Et l'application reste **figée pour toujours**.

La raison : Hosting ne relaie pas les connexions **WebSocket**. Or Streamlit ne
communique avec le navigateur *que* par WebSocket — chaque clic, chaque menu,
chaque résultat passe par là. La page initiale arrive en HTTP, puis plus rien.

C'est un problème connu, rencontré par tous les frameworks du même type
(Streamlit, NiceGUI, Dash…) derrière Hosting. Il n'existe pas de contournement
côté configuration : ce n'est pas un réglage à trouver, c'est une limite de
l'infrastructure.

**Conséquence pratique : l'application doit être servie directement par Cloud
Run, sur son URL `*.run.app`.** Firebase Hosting garde un rôle utile, mais
différent — voir plus bas.

---

## Ce que Firebase Hosting fait ici (et c'est gratuit)

Une page d'accueil qui **redirige** vers l'application. Vous obtenez une adresse
courte et mémorisable — `pronostat-8a2a8.web.app` — qui renvoie vers l'app, où
qu'elle soit hébergée. Le WebSocket s'établit ensuite en direct avec la vraie
adresse, sans passer par Hosting : plus de blocage.

Cela fonctionne sur le **plan Spark, gratuit, sans carte bancaire**.

Marche à suivre :

1. Ouvrez `public/app-url.js`
2. Collez l'URL de l'application entre les guillemets, par exemple
   `"https://pronostat.streamlit.app"`
3. Publiez :

```bash
firebase deploy --only hosting
```

Laissée vide, la page affiche simplement un message expliquant que l'adresse
n'est pas renseignée — elle ne redirige pas dans le vide.

> La CLI Firebase est déjà installée sur cette machine. Si `firebase login` n'a
> jamais été fait, la commande vous le demandera et ouvrira le navigateur.

---

## Déployer sur Cloud Run

### Ce qu'il faut avant de commencer

| Prérequis | Pourquoi | Coût |
|---|---|---|
| Compte de facturation Google Cloud | Cloud Run l'exige, même pour le palier gratuit. Le projet passe en **Blaze** | Carte obligatoire, usage réel ≈ 0 € |
| `gcloud` CLI | Non installé sur cette machine. <https://cloud.google.com/sdk/docs/install> | Gratuit |

Le palier toujours-gratuit de Cloud Run (2 millions de requêtes/mois, 180 000
vCPU-secondes) dépasse très largement l'usage d'une application personnelle.
La facture réelle devrait rester nulle — mais **une carte est exigée pour
activer le service**, et c'est à vous de la poser : personne ne peut le faire à
votre place.

`docker` n'est pas nécessaire : l'option `--source` fait construire l'image par
Cloud Build, côté Google.

### La commande

```bash
gcloud run deploy pronostat --source . --project pronostat-8a2a8 --region europe-west1 --allow-unauthenticated --port 8080 --memory 1Gi --timeout 3600 --session-affinity --min-instances 0 --max-instances 2
```

Chaque option non évidente, et pourquoi elle est là :

| Option | Raison |
|---|---|
| `--timeout 3600` | **Critique.** Le WebSocket de Streamlit est une connexion longue. Avec le défaut (300 s), l'application se déconnecte toutes les 5 minutes |
| `--session-affinity` | **Critique.** Streamlit garde l'état de session en mémoire de l'instance. Sans affinité, un clic peut atterrir sur une autre instance qui ne connaît pas votre session |
| `--memory 1Gi` | Le Monte Carlo à 20 000 simulations avec numpy dépasse les 512 Mio par défaut |
| `--min-instances 0` | Reste dans le palier gratuit. Contrepartie : démarrage à froid de 10 à 30 s après une période d'inactivité |
| `--max-instances 2` | Garde-fou contre une facture surprise |
| `--allow-unauthenticated` | Sans cela, l'URL exige un jeton Google et le navigateur reçoit un 403 |

### Les clés API

Cloud Run ne lit pas votre `.env` — il n'est pas envoyé (voir `.dockerignore`).
Passez les variables au service :

```bash
gcloud run services update pronostat --project pronostat-8a2a8 --region europe-west1 --update-env-vars PRONOSTAT_ENV=production,PRONOSTAT_CACHE_DIR=/tmp/pronostat-cache,ODDS_API_KEY=votre_cle,THESPORTSDB_API_KEY=3,ODDS_REGIONS=eu,MARKET_WEIGHT=0.60,MC_SIMS=20000
```

Une clé posée ainsi reste visible dans la console Cloud Run et dans l'historique
de votre terminal. Pour une gestion plus stricte, passez par Secret Manager :

```bash
gcloud run services update pronostat --project pronostat-8a2a8 --region europe-west1 --update-secrets ODDS_API_KEY=odds-api-key:latest
```

### Après le déploiement

La commande affiche l'URL du service, de la forme
`https://pronostat-xxxxxxxxxx-ew.a.run.app`. **C'est cette adresse qu'il faut
ouvrir** — et c'est elle qu'il faut coller dans `public/app-url.js` si vous
voulez que l'adresse Firebase y renvoie.

Vérifiez que l'interface réagit — un menu déroulant qui s'ouvre et se referme
suffit à prouver que le WebSocket est établi.

---

## Streamlit Cloud ou Cloud Run : ce qui les sépare

| | Streamlit Cloud | Cloud Run |
|---|---|---|
| Carte bancaire | Non | **Oui** (usage ≈ 0 €) |
| Mise en ligne | ~5 min, par le navigateur | ~20 min, `gcloud` à installer |
| Mise à jour | Automatique à chaque `git push` | Relancer `gcloud run deploy` |
| Veille | S'endort après quelques jours sans visite | Démarrage à froid de 10-30 s |
| Mémoire | 1 Gio, non réglable | Réglable |
| Confidentialité | Héritée du dépôt GitHub | `--allow-unauthenticated` ou IAM |

Pour un usage personnel, Streamlit Cloud suffit et ne coûte rien. Cloud Run
devient intéressant si vous voulez maîtriser la mémoire, éviter la mise en
veille, ou faire passer l'application derrière votre propre domaine.

---

## Limite commune aux deux : l'historique n'est pas permanent

`ledger.json` et `history.json` sont écrits sur le disque du serveur. Ce disque
est **éphémère** dans les deux cas : effacé à chaque redémarrage sur Streamlit
Cloud, et à chaque nouvelle instance sur Cloud Run (`/tmp` vit en mémoire).

Vos analyses passées et la calibration repartent alors de zéro. Ce n'est pas un
défaut du code, c'est la nature de l'hébergement sans disque persistant. Y
remédier demande une base externe — Firestore serait le choix cohérent ici,
puisque le projet Firebase existe déjà — et une modification de
`agent/memory.py`.
