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
                    # ── Events forwarded to the customer waiting/tracker page ──
                    #
                    # status_changed: standard order-status updates (pending →
                    #   preparing → ready → completed).  The payment_waiting page
                    #   JS uses this as a fallback to detect when the order has
                    #   been accepted (status moved past pending).
                    #
                    # payment_confirmed: fired by process_payment when the cashier
                    #   marks a PENDING customer order as paid (is_paid→True, status
                    #   stays PENDING).  Lets the waiting page transition from
                    #   "Payment Required" to "Payment Confirmed — waiting for staff
                    #   acceptance".
                    #
                    # order_accepted: fired by accept_order when the cashier moves
                    #   a paid PENDING order → PREPARING.  Tells the waiting page to
                    #   show the "Order Received" overlay and redirect to the tracker.
                    #
                    # All other event types (new_order, inventory_changed,
                    # inventory_low) are irrelevant to the customer tracker
                    # and must never be sent to unauthenticated clients.

                    evt  = event['event']
                    data = event['data']

                    if evt == 'status_changed' and data.get('order_number') == order_number:
                        payload = {
                            'order_number':       data['order_number'],
                            'queue_number':       data['queue_number'],
                            'new_status':         data['new_status'],
                            'new_status_display': data['new_status_display'],
                            'is_paid':            data.get('is_paid', False),
                        }
                        yield format_sse('status_changed', payload)

                    elif evt == 'payment_confirmed' and data.get('order_number') == order_number:
                        payload = {
                            'order_number': data['order_number'],
                            'is_paid':      data.get('is_paid', True),
                            'status':       data.get('status', ''),
                        }
                        yield format_sse('payment_confirmed', payload)

                    elif evt == 'order_accepted' and data.get('order_number') == order_number:
                        payload = {
                            'order_number':       data['order_number'],
                            'new_status':         data.get('new_status', 'preparing'),
                            'new_status_display': data.get('new_status_display', 'Preparing'),
                        }
                        yield format_sse('order_accepted', payload)

                    elif evt == 'gcash_rejected' and data.get('order_number') == order_number:
                        # Tell the customer their submission was rejected so the
                        # page can transition back to the reference submission form.
                        payload = {
                            'order_number':   data['order_number'],
                            'rejection_note': data.get('rejection_note', ''),
                        }
                        yield format_sse('gcash_rejected', payload)

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
