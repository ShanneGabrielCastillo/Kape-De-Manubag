# Gunicorn configuration for Kape De Manubag on Render.
#
# WHY GEVENT:
# Dashboard, POS, and Order Management all use Server-Sent Events (SSE).
# Each SSE connection holds an open HTTP connection for its lifetime.
# With the default sync worker, one SSE connection occupies one worker
# thread — so on Render's free tier (WEB_CONCURRENCY=1, one sync worker),
# a single open SSE tab blocks ALL other page requests until it closes.
# This was causing the 10–30s delays when navigating between those pages.
#
# Gevent workers use cooperative green threads: hundreds of SSE connections
# share one OS thread without blocking each other or blocking page requests.
# The SSE broker (apps/realtime/broker.py) is already thread-safe (uses
# threading.Lock), which is compatible with gevent's monkey-patching.

import os

# Worker class: gevent handles SSE / long-poll connections without blocking.
worker_class = "gevent"

# Number of gevent green threads per worker. 100 means one worker can
# handle 100 concurrent SSE connections + page requests simultaneously.
# This is more than enough for a single café deployment.
worker_connections = 100

# Workers: Render sets WEB_CONCURRENCY automatically (1 on free tier).
# We read it here so the config works on paid tiers too without changes.
workers = int(os.environ.get("WEB_CONCURRENCY", 1))

# Timeouts: gevent workers use async I/O so the default 30s worker timeout
# would incorrectly kill long-lived SSE connections. Set to 0 to disable
# the worker timeout entirely for gevent (it handles its own keep-alive).
timeout = 0

# Keep-alive: how long to wait for the next request on a keep-alive connection.
keepalive = 5

# Bind address is set by Render via the PORT environment variable.
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
