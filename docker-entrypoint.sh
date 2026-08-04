#!/bin/sh
# First boot: create settings.json from the Docker template so the stack
# works out of the box. Secrets (TOKEN, CLIENT_ID, OPENAI_API_KEY) come
# from the environment via the compose .env file.
set -e
cd /app

if [ ! -f settings.json ]; then
    echo "No settings.json found - creating one from settings.docker.json"
    cp "settings.docker.json" settings.json
fi

if [ -z "$TOKEN" ]; then
    echo "ERROR: TOKEN is not set. Copy .env.example to .env and fill in your bot token." >&2
    exit 1
fi

exec python -u main.py
