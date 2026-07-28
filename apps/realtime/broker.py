"""
Lightweight in-memory event broker for Server-Sent Events.

Works correctly for single-worker-process WSGI deployment
(PythonAnywhere default). If deployment ever scales to
multiple worker processes, this would need to migrate to
Redis pub/sub — not needed at current scale.
"""
import queue
import threading
import time

_lock = threading.Lock()
_subscribers = []


def subscribe():
    q = queue.Queue(maxsize=50)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q):
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def publish(event_type, data):
    payload = {
        'event':     event_type,
        'data':      data,
        'timestamp': time.time(),
    }
    with _lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except queue.Empty:
                    pass


def subscriber_count():
    with _lock:
        return len(_subscribers)
