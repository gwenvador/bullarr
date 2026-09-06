# Base "slim" plutôt que l'image Debian complète (docs/man/locales en moins, mêmes
# paquets disponibles via apt) - même famille d'image, juste sans le superflu qu'on
# n'utilise jamais dans un conteneur.
FROM debian:trixie-slim

# Configurer les sources APT (contrib/non-free pour unrar, amule-utils), installer les
# dépendances système, PUIS nettoyer l'index apt - le tout dans un seul RUN. Un cleanup
# dans une couche séparée ne réduit pas la taille de l'image (les fichiers restent dans
# la couche apt-get update précédente, juste masqués) ; il faut que création et suppression
# soient dans la même couche pour que ça compte vraiment.
RUN echo "deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware" > /etc/apt/sources.list && \
    echo "deb http://deb.debian.org/debian-security trixie-security main contrib non-free non-free-firmware" >> /etc/apt/sources.list && \
    apt-get update -o Acquire::Retries=3 -o Acquire::http::timeout=60 && \
    apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    p7zip-full \
    unrar \
    # ^ le vrai unrar (non-free), PAS unrar-free: rarfile ne sait pas piper à travers
    #   unrar-free (conversion cbr->cbz cassée avec "Failed the read enough data")
    ca-certificates \
    curl \
    amule-utils && \
    rm -rf /var/lib/apt/lists/*

# Définir le répertoire de travail
WORKDIR /app

# Copier les requirements et installer les dépendances Python
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copier les fichiers Python critiques
COPY app.py .
COPY config.py .
COPY encryption.py .
COPY rename_handler.py .
COPY run.py .
COPY docker-entrypoint.sh .

# Copier les blueprints
COPY blueprints/ ./blueprints/

# Copier les dossiers statiques et templates
COPY static/ ./static/
COPY templates/ ./templates/

# Copier les autres fichiers de configuration
COPY . .

# Rendre le script d'entrypoint exécutable
RUN chmod +x docker-entrypoint.sh

# Créer les répertoires nécessaires
RUN mkdir -p data/covers

# Exposer le port de l'application
EXPOSE 5000

# Définir les variables d'environnement
ENV FLASK_APP=app.py
ENV PYTHONUNBUFFERED=1

# Utiliser le script d'entrypoint (exécuté en tant que root)
ENTRYPOINT ["./docker-entrypoint.sh"]
