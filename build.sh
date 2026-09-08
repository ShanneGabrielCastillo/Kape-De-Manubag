#!/usr/bin/env bash
# Render build script — runs on every deploy.
# Exit immediately on any error so a failed deploy is caught early.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

python manage.py collectstatic --noinput
python manage.py migrate
