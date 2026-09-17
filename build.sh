#!/usr/bin/env bash
# Render build script — runs on every deploy.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

python manage.py collectstatic --noinput
python manage.py migrate

# Create superuser from env vars if it doesn't exist yet (idempotent).
python manage.py create_superuser_env

# NOTE: The Render start command must be:
#   gunicorn kape_de_manubag.wsgi:application -c gunicorn.conf.py
# gunicorn.conf.py configures gevent workers so SSE connections (Dashboard,
# POS, Order Management) don't block page requests on Render's free tier.

# Load initial data from PythonAnywhere export (one-time seed).
# This file is removed from the repo after the first successful load.
if [ -f data_export.json ]; then
  echo "==> Loading data from data_export.json..."
  python manage.py loaddata data_export.json
  echo "==> Data loaded successfully."
fi

# Load data from data.json if present (one-time seed from PythonAnywhere).
if [ -f data.json ]; then
  echo "==> Loading data from data.json..."
  python manage.py loaddata data.json
  echo "==> data.json loaded successfully."
fi
