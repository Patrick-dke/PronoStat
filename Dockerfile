# Image de production pour PronoStat.
# Sert à déployer partout où l'on peut exécuter un conteneur : Google Cloud
# Run (derrière Firebase Hosting), Render, Fly.io, Azure Container Apps…
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PRONOSTAT_ENV=production

WORKDIR /app

# Les dépendances d'abord : cette couche est réutilisée tant que
# requirements.txt ne change pas, ce qui accélère les redéploiements.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Le disque du conteneur est éphémère : le cache va dans /tmp, toujours
# accessible en écriture.
ENV PRONOSTAT_CACHE_DIR=/tmp/pronostat-cache

# Cloud Run impose le port via $PORT ; 8080 sert de valeur par défaut locale.
ENV PORT=8080
EXPOSE 8080

# `sh -c` est nécessaire pour que $PORT soit développé au démarrage.
CMD ["sh", "-c", "streamlit run app.py \
     --server.port=${PORT} \
     --server.address=0.0.0.0 \
     --server.headless=true \
     --server.enableCORS=false \
     --server.enableXsrfProtection=true \
     --browser.gatherUsageStats=false"]
