# Image de l'API PronoStat pour Hugging Face Spaces.
#
# Distincte du Dockerfile racine, qui sert l'interface Streamlit : les deux
# services partagent le moteur mais pas leur point d'entrée.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PRONOSTAT_ENV=production \
    PRONOSTAT_LOG_LEVEL=WARNING

# Hugging Face n'autorise l'écriture que dans /tmp. Le cache y est déjà
# dirigé par défaut dans config.py, on le rend explicite ici.
ENV PRONOSTAT_CACHE_DIR=/tmp/pronostat-cache

WORKDIR /app

# Les dépendances d'abord : cette couche est réutilisée tant que les
# fichiers requirements ne changent pas, ce qui accélère les reconstructions.
COPY requirements.txt requirements-api.txt ./
RUN pip install --no-cache-dir -r requirements-api.txt

COPY . .

# Hugging Face expose le port 7860 par défaut. Il doit correspondre au
# champ `app_port` déclaré dans le README du Space.
ENV PORT=7860
EXPOSE 7860

# Un seul worker : le palier gratuit est limité en mémoire, et le Monte
# Carlo à 20 000 tirages avec numpy en consomme une part notable.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT} --workers 1"]
