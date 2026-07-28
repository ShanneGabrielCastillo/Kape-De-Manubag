"""
Server-Sent Events streaming endpoint for staff dashboards.
Customer-facing pages do NOT use this endpoint.
"""
import json
import time
import queue

from django.http import StreamingHttpResponse
from django.contrib.auth.decorators import login_required

from apps.accounts.decorators import cashier_or_admin_required
from apps.realtime.broker import subscribe, unsubscribe


@login_required
@cashier_or_admin_required
def event_stream(request):

    def stream():
        client_queue = subscribe()
        last_heartbeat = time.time()
        try:
            while True:
                try:
                    event = client_queue.get(timeout=15)
                    yield format_sse(event['event'], event['data'])
                except queue.Empty:
                    pass
                if time.time() - last_heartbeat >= 20:
                    yield format_sse('heartbeat', {'timestamp': time.time()})
                    last_heartbeat = time.time()
        except GeneratorExit:
            unsubscribe(client_queue)
            raise

    response = StreamingHttpResponse(
        stream(),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


def format_sse(event_type, data):
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
