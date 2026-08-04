# Rendre l'historique permanent (Firestore)

Sans cette configuration, l'application fonctionne — mais son journal vit sur
le disque de l'hébergeur, **effacé à chaque redémarrage**. Vos analyses
passées disparaissent, et le taux de réussite repart de zéro sans jamais
atteindre les 20 résultats nécessaires pour signifier quelque chose.

Firestore est **gratuit sur le plan Spark** : pas de carte bancaire, contrairement
à Cloud Run. Le quota offert — 50 000 lectures et 20 000 écritures par jour —
est sans commune mesure avec l'usage ici : le journal tient dans **un seul
document**, donc une lecture et une écriture par analyse.

Comptez cinq minutes, en trois étapes.

---

## 1. Activer Firestore

1. Ouvrez la [console Firebase](https://console.firebase.google.com/project/pronostat-8a2a8/firestore)
2. Cliquez **Créer une base de données**
3. Choisissez **Mode production** — les règles de sécurité n'ont aucune
   importance ici, l'application se connecte avec un compte de service qui les
   contourne par conception. Le mode test ouvrirait votre base à tout internet
   pendant 30 jours : ne le prenez pas.
4. Choisissez la région **eur3 (europe-west)** ou la plus proche de vous.
   **Ce choix est définitif** — il ne peut plus être modifié ensuite.

---

## 2. Créer une clé de compte de service

1. Toujours dans la console : **⚙️ Paramètres du projet** → onglet
   **Comptes de service**
2. Cliquez **Générer une nouvelle clé privée**, puis confirmez
3. Un fichier `.json` se télécharge

> ⚠️ **Ce fichier donne un accès complet à votre projet Firebase.** Ne le
> déposez jamais dans le dépôt — `Patrick-dke/PronoStat` est public. Il n'a
> sa place que dans les secrets Streamlit, et nulle part ailleurs.

---

## 3. Le coller dans les secrets

Ouvrez le fichier téléchargé dans le Bloc-notes, copiez **tout son contenu**.

Puis sur [share.streamlit.io](https://share.streamlit.io) → votre application →
**⋮ → Settings → Secrets**, ajoutez ceci **à la suite** de ce qui s'y trouve
déjà, sans rien effacer :

```toml
FIREBASE_SERVICE_ACCOUNT = '''
{
  "type": "service_account",
  "project_id": "pronostat-8a2a8",
  ... collez ici tout le contenu du fichier ...
}
'''
```

Trois détails qui font échouer le reste du temps :

- **Trois apostrophes** `'''` avant et après — pas des guillemets. C'est la
  seule façon d'écrire un texte sur plusieurs lignes en TOML.
- **Le JSON entier**, accolades comprises, entre ces apostrophes.
- **Ne supprimez pas** `ODDS_API_KEY` ni les autres lignes déjà présentes.

Cliquez **Save**. L'application redémarre seule.

---

## Vérifier que ça marche

Lancez une analyse, puis dépliez **« Mes analyses précédentes »**. La ligne
sous le titre indique où le journal est conservé :

| Ce qui s'affiche | Ce que ça veut dire |
|---|---|
| *Journal conservé dans **Firestore**. Il survit aux redémarrages.* | ✅ c'est bon |
| *Journal conservé dans **fichier local** ⚠️* | la configuration n'est pas prise en compte |

Dans le second cas, la cause est presque toujours le format : apostrophes
simples au lieu de triples, ou JSON tronqué à la copie.

---

## Ce que ça change concrètement

Le taux de réussite ne s'affiche qu'à partir de **20 résultats connus**. Sur
un disque éphémère ce seuil est hors d'atteinte : chaque redémarrage remet le
compteur à zéro. Avec Firestore, vos analyses s'accumulent semaine après
semaine, et le taux affiché devient une mesure réelle de la méthode — la
vôtre, constatée, pas une promesse invérifiable.

---

## Si vous préférez ne rien configurer

L'application continue de fonctionner exactement comme aujourd'hui. Vous
perdez seulement la mémoire longue : les pronostics du jour restent visibles,
le taux de réussite ne se construit pas. Rien d'autre ne change.
