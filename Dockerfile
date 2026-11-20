# Dockerfile — Kape De Manubag Food Ordering & Management System
# Local development image: Django development server on port 8000.
#
# Runtime data (db.sqlite3, media/, logs/, staticfiles/) is intentionally
# NOT baked into the image. It is provided at runtime by the bind mount in
# compose.yaml, so the existing database and uploaded files are always
# preserved on the host. Secrets (.env) are also excluded from the build
# context via .dockerignore and injected through compose env_file.

FROM python:3.11.9-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
# All pinned requirements ship manylinux wheels for cp311 (psycopg uses the
# [binary] extra, Pillow wheels bundle their codecs), so no compiler
# toolchain or system libraries are required on the slim base image.
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copy application code (.env, db.sqlite3*, media/, venv*/, .git/ are
# excluded by .dockerignore and supplied at runtime via the bind mount).
COPY . .

EXPOSE 8000

# Development server for the initial local development configuration. With
# DEBUG=True (from .env) it serves /static/ and /media/ automatically and
# auto-reloads on code changes. runserver never applies migrations.
#
# For a production-style launch later, switch the command to Gunicorn:
#   gunicorn --config gunicorn.conf.py kape_de_manubag.wsgi:application
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
