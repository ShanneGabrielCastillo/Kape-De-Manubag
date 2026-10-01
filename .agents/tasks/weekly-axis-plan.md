# Implementation Plan — Sales Overview Weekly X-Axis Sun–Sat

## Findings from Code Inspection

### Root Cause

There are **two intertwined bugs** in `apps/dashboard/views.py` → `_chart_series()`:

1. **Partial-week window** — the loop iterates from `start` to `today` inclusive
   (`range((today - start).days + 1)`). If today is Wednesday, only 4 data points
   are generated (Sun, Mon, Tue, Wed). The requirement is always 7 days: Sun → Sat.

2. **Wrong label format on the initial page render** — `dashboard_index` calls
   `_load_widgets('week', chart_label_fmt='%b %d', ...)`, so the initial chart
   labels look like "Jun 15", "Jun 16" … rather than "Sun", "Mon" …  
   The `/dashboard/chart-data/` AJAX endpoint does use `label_fmt='%a'` (weekday
   abbreviation) correctly via the default in `_chart_series`, but the initial
   server-rendered labels diverge from what a period-switch would show.

3. **No upper-bound filter** — `created_at__date__gte=start` fetches all orders
   from Sunday onward with no `__lte=week_end` guard. For week view this means
   future orders (if any existed beyond today) would appear, and it is
   inconsistent with building exactly 7 labelled buckets.

**Bug 1 is the primary functional defect.** On any day other than Saturday the
chart is shorter than 7 days, causing wrong X-axis labels and missing bars.

### What the current code does

| Location | What it does |
|---|---|
| `_chart_series('week')` | Loops `start … today` only → partial week |
| `dashboard_index` | Passes `chart_label_fmt='%b %d'` → "Jun 15" labels on first load |
| `/dashboard/chart-data/?period=week` | Uses default `label_fmt='%a'` → "Mon" labels on AJAX refresh |
| `_period_start('week', today)` | `today - timedelta(days=today.isoweekday() % 7)` → correct Sunday start |
| `TruncDate` + `USE_TZ=True` + `TIME_ZONE='Asia/Manila'` | Timezone is handled correctly by Django |

### JSON structure returned by `/dashboard/chart-data/`
```json
{ "labels": ["Sun", "Mon", "Tue", "Wed"], "data": [0.0, 1250.0, 980.0, 1540.0] }
```
After the fix it should always return exactly 7 labels and 7 data values.

### What is NOT changing
- `_period_start()` — the Sunday calculation is already correct
- `_sales_stats()` — not touched
- `_top_products()` — not touched
- Month logic (`period == 'month'`) — not touched; it iterates start-to-today which is correct for month view
- Database models, migrations, orders, finance, sales reports, Top Products — not touched
- Chart type (already `bar`), tooltips, styling, responsive behaviour — not touched
- The realtime summary endpoint (`dashboard_summary`) — not touched except it will receive correct chart data from the fixed `_load_widgets`

---

## Implementation Plan

- [ ] 1. Fix `_chart_series()` in `apps/dashboard/views.py` to always iterate all 7 days of the Sun–Sat week when `period == 'week'`, and fix `dashboard_index` to pass `label_fmt='%a'` for the weekly initial render.

   **What to do:**

   a. In `_chart_series`, change the loop end from `today` to `week_end` when the period is `'week'`. Define `week_end = start + timedelta(days=6)` (always Saturday). For `'month'` keep the loop end as `today` (no change).

   b. Add `created_at__date__lte=week_end` to the ORM filter when period is `'week'` so the query is bounded to exactly the 7-day window (defensive; no functional difference in practice since future sales do not exist, but it makes intent explicit).

   c. In `dashboard_index`, change `chart_label_fmt='%b %d'` to `chart_label_fmt=None` (which lets `_chart_series` default to `'%a'` for week — "Sun", "Mon" … — and `'%b %d'` for month). This fixes the label mismatch between the initial render and a subsequent period-switch.

   **Exact diff for `_chart_series`** (replace the function body):

   ```python
   def _chart_series(period='week', label_fmt=None):
       today = timezone.localdate()
       start = _period_start(period, today)

       if period == 'week':
           end = start + timedelta(days=6)   # always Saturday
       else:
           end = today                        # month: up to today only

       if label_fmt is None:
           label_fmt = '%b %d' if period == 'month' else '%a'

       # One grouped query over the exact date window.
       filter_kwargs = dict(is_paid=True, status='completed',
                            created_at__date__gte=start,
                            created_at__date__lte=end)
       day_sales = (
           Order.objects.filter(**filter_kwargs)
           .annotate(day=TruncDate('created_at'))
           .values('day')
           .annotate(total=Sum('total'))
       )
       sales_by_day = {row['day']: row['total'] for row in day_sales}

       labels = []
       data = []
       for i in range((end - start).days + 1):
           day = start + timedelta(days=i)
           labels.append(day.strftime(label_fmt))
           data.append(float(sales_by_day.get(day, 0) or 0))
       return labels, data
   ```

   **Exact diff for `dashboard_index`** — change the one `_load_widgets` call:

   ```python
   # Before:
   widgets, widget_errors = _load_widgets(
       'week', chart_label_fmt='%b %d', include_recent=True,
   )

   # After:
   widgets, widget_errors = _load_widgets(
       'week', chart_label_fmt=None, include_recent=True,
   )
   ```

   Note: `_load_widgets` already passes `chart_label_fmt` through to `_chart_series` via the lambda `lambda: _chart_series(period, label_fmt=chart_label_fmt)`. Passing `None` means `_chart_series` picks `'%a'` for week and `'%b %d'` for month — exactly what we want.

   **Files:** `apps/dashboard/views.py`

   **Verify:**
   ```
   python manage.py test apps.dashboard --verbosity=2
   ```
   All existing tests must pass. Pay particular attention to `test_chart_data_covers_last_seven_days` — **this test will also need updating** (see item 2 below).

---

- [ ] 2. Update the existing chart test in `apps/dashboard/tests.py` to assert the new Sun–Sat 7-day contract.

   The test `DashboardStatisticsTests.test_chart_data_covers_last_seven_days` currently:
   - computes expected values by iterating `today` back 6 days (a rolling window)
   - does NOT assert the weekday labels

   After the fix the chart always covers exactly `week_start … week_start+6` (Sun–Sat), so the test must be rewritten to match.

   **What to do:** Replace `test_chart_data_covers_last_seven_days` with a test that:
   1. Computes `week_start = today - timedelta(days=today.isoweekday() % 7)`.
   2. Asserts `len(labels) == 7` and `len(data) == 7`.
   3. Asserts `labels == ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']`.
   4. Asserts each `data[i]` matches the total for `week_start + timedelta(days=i)`.

   Also add a new test `test_chart_data_week_always_seven_days` that hits the
   `/dashboard/chart-data/?period=week` endpoint directly and asserts:
   - exactly 7 labels
   - labels are `['Sun','Mon','Tue','Wed','Thu','Fri','Sat']`
   - all 7 data values are floats ≥ 0

   **Files:** `apps/dashboard/tests.py`

   **Verify:**
   ```
   python manage.py test apps.dashboard --verbosity=2
   ```
   All tests pass, including the rewritten and new chart tests.

---

## How each requirement is satisfied

| Requirement | How it is met |
|---|---|
| Always 7 days Sun–Sat | Loop from `start` to `start + 6` instead of `start` to `today` |
| Sunday start | `_period_start('week')` already computes `today - timedelta(days=today.isoweekday() % 7)` — unchanged |
| Labels ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"] | `label_fmt='%a'` + `start` = Sunday → `strftime('%a')` produces exactly these abbreviations |
| Missing days = ₱0 | `sales_by_day.get(day, 0) or 0` — days with no orders default to 0.0 |
| Month behaviour unchanged | `period == 'month'` branch keeps `end = today`; `label_fmt` defaults to `'%b %d'` — identical to before |
| No model/migration changes | Only `_chart_series` and `dashboard_index` are modified |
| Data grouped by Asia/Manila date | `TruncDate` + `USE_TZ=True` + `TIME_ZONE='Asia/Manila'` in settings — already correct, not changed |
| All other dashboard widgets untouched | `_sales_stats`, `_top_products`, `_low_stock`, `_recent_orders`, `_status_counts` — not touched |

---

## Surprises / Complications Found

1. **Initial-render label mismatch** — `dashboard_index` was passing `chart_label_fmt='%b %d'` for the initial week render. This means the first paint showed date labels ("Jun 15") but the AJAX refresh after clicking Week→Month→Week would show weekday labels ("Sun"). The fix (`chart_label_fmt=None`) unifies both paths.

2. **Existing test uses a rolling window** — `test_chart_data_covers_last_seven_days` was written against the old rolling-7-day assumption. It must be rewritten (item 2), not just run as a regression check.

3. **`_sales_stats` weekly aggregation is correct and consistent** — `week_start = today - timedelta(days=today.isoweekday() % 7)` with no upper bound is fine for the stat-tile sum (it counts all orders this week through today). The chart fix does not need to change this.

4. **`TruncDate` timezone** — Django's `TruncDate` respects `TIME_ZONE='Asia/Manila'` when `USE_TZ=True`, so all calendar-date grouping is already in the correct timezone. No manual conversion is needed.

5. **No second API, no new endpoints** — all changes are inside the existing `_chart_series` function and one call site. The `/dashboard/chart-data/` URL and JSON shape are unchanged.
