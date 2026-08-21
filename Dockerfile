# Image unique : l'app et sa boucle de scan dans le même processus.
# Le navigateur (Facebook) n'est PAS embarqué : il a besoin de ta session
# personnelle, qui ne se met pas dans une image. Voir README, section homelab.
FROM python:3.12-slim

# tzdata : les heures de fin d'enchère et « il y a 2h » doivent être justes.
# curl : pour le HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Europe/Zurich

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Tout l'état muable vit ici : base, profil navigateur, journaux. À monter en
# volume, sinon une reconstruction d'image efface tes recherches.
VOLUME ["/app/data"]
ENV PORT=5055 HOST=0.0.0.0 BROWSER_HEADLESS=1
EXPOSE 5055

# Pas de root : si l'app est exposée, autant qu'elle n'ait rien à donner.
RUN useradd -u 10001 -m seconde && chown -R seconde:seconde /app
USER seconde

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/status" || exit 1

CMD ["python3", "app.py"]
