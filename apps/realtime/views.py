"""
Server-Sent Events streaming endpoints.

``event_stream``           — staff/cashier dashboard (requires login + role).
``customer_order_stream``  — customer-facing tracker (public, filtered to one
                             order number so a customer only receives updates
                             for their own order).
"""
import json
import time
import queue

from django.http import StreamingHttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_GET

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


@require_GET
def customer_order_stream(request):
    """Public SSE endpoint for the customer-facing order tracker.

    The client connects with ``?tracking_token=<token>``.  The token is the
    cryptographically random tracking_token stored on the Order, so this
    endpoint cannot be enumerated from the predictable order_number.

    Internally, the in-memory broker publishes events keyed by order_number
    (that is an internal implementation detail that does not change).  We look
    up the order_number from the token once at connection time, then filter the
    broker stream by that number — customers only see updates for their own
    order.

    Design notes
    ─────────────
    • No authentication required — the tracking_token acts as the bearer
      credential, consistent with how ``order_tracker`` and ``api_track_order``
      work.
    • Heartbeats every 20 s keep the connection alive through proxies /
      mobile networks that close idle streams.
    • On ``GeneratorExit`` (client disconnects) the broker queue is removed
      so the in-memory subscriber list stays clean.
    • The endpoint returns JSON 400/404 on bad/missing tokens so the JS can
      distinguish configuration errors from connection drops.
    """
    tracking_token = request.GET.get('tracking_token', '').strip()
    if not tracking_token:
        return JsonResponse({'error': 'tracking_token is required'}, status=400)

    # Validate the token and resolve to the internal order_number used by the
    # broker.  We fetch only the two fields we need to keep this query cheap.
    from apps.orders.models import Order
    order_qs = Order.objects.filter(tracking_token=tracking_token).values('order_number')
    order_row = order_qs.first()
    if not order_row:
        return JsonResponse({'error': 'Order not found'}, status=404)

    # Internal identifier used by the broker — never sent to the client.
    order_number = order_row['order_number']

    def stream():
        client_queue = subscribe()
        last_heartbeat = time.time()
        try:
            while True:
                try:
                    event = client_queue.get(timeout=15)
                    # Forward only status_changed events for THIS order.
                    # All other event types (new_order, inventory_changed,
                    # inventory_low) are irrelevant to the customer tracker
                    # and must never be sent to unauthenticated clients.
                    if (
                        event['event'] == 'status_changed'
                        and event['data'].get('order_number') == order_number
                    ):
                        # Only expose the fields the tracker UI needs.
                        # Payment amounts, cashier info, and internal flags
                        # are deliberately excluded.
                        payload = {
                            'order_number':       event['data']['order_number'],
                            'queue_number':       event['data']['queue_number'],
                            'new_status':         event['data']['new_status'],
                            'new_status_display': event['data']['new_status_display'],
                        }
                        yield format_sse('status_changed', payload)
                except queue.Empty:
                    pass
                # Keep the TCP connection alive.
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
