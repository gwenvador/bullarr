#!/bin/bash
set -e

# Script de démarrage du container

# Vérifier que amulecmd est disponible
if ! command -v amulecmd &> /dev/null; then
    echo "⚠️ Avertissement : amulecmd n'a pas pu être installé"
    echo "L'intégration aMule ne sera pas disponible"
fi

# Start a production WSGI master. A Gunicorn hook owns exactly one independent
# scheduler process; web workers never start duplicate APScheduler instances.
exec gunicorn -c /app/gunicorn.conf.py wsgi:application
