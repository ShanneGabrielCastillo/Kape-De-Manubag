# Gunicorn configuration for Kape De Manubag on Render.
#
# WHY GTHREAD (not gevent):
# Dashboard, POS, and Order Management all use Server-Sent Events (SSE).
# With the default sync worker, one SSE connection occupies the entire
# worker — so on Render's free tier (WEB_CONCURRENCY=1), a single open
# SSE tab blocks ALL page requests, causing 10-30s delays on navigation.
#
# gthread workers give each request its own OS thread from a thread pool,
# so SSE connections and normal page requests run concurrently without
# blocking each other. Unlike gevent, gthread does NOT monkey-patch Python,
# so it is fully compatible with psycopg (the async Postgres driver we use).
# gevent was tried first but caused DatabaseError: "DatabaseWrapper objects
# created in a thread can only be used in that same thread" because gevent's
# monkey-patching conflicts with psycopg's internal connection model.

import os

# Worker class: gthread — OS threads, no monkey-patching, psycopg-safe.
worker_class = "gthread"

# Threads per worker: each thread handles one request (or one SSE connection).
# 4 threads means 4 concurrent requests can be served simultaneously,
# which is enough for Dashboard + POS + Order Management all open at once
# plus normal page navigations. Free-tier memory allows this comfortably.
threads = 4

# Workers: Render sets WEB_CONCURRENCY automatically (1 on free tier).
workers = int(os.environ.get("WEB_CONCURRENCY", 1))

# Timeout: gthread workers must not time out SSE connections (which are
# intentionally long-lived). Set to 0 to disable the worker timeout.
timeout = 0

# Keep-alive: how long to wait for the next request on a keep-alive connection.
keepalive = 5

# Bind address: Render exposes the port via the PORT environment variable.
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
