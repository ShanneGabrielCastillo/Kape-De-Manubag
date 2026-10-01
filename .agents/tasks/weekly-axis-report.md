# Implementation Report — Sales Overview Weekly X-Axis Sun–Sat Fix

**Commit:** `1b4841d` — `fix: weekly chart always shows Sun-Sat 7 days in Asia/Manila week`  
**Date:** Thu Oct 1 16:24:51 2026 +0800  
**Files changed:** `apps/dashboard/views.py`, `apps/dashboard/tests.py`

---

## 1. Root Cause of the Pre-Fix Behavior

There were two intertwined bugs in `apps/dashboard/views.py` → `_chart_series()`:

### Bug 1 — Partial-week window (primary defect)

The loop that builds the chart labels and data iterated from `start` (Sunday) up to **`today`** inclusive:

```python
for i in range((today - start).days + 1):
```

If today was Wednesday, the range was `(Wed − Sun).days + 1 = 4`, producing only four bars: Sun, Mon, Tue, Wed. Thu, Fri, and Sat were never generated. The chart was always shorter than 7 days unless it happened to be Saturday.

### Bug 2 — Mismatched label format on the initial page render (secondary defect)

`dashboard_index` called `_load_widgets` with `chart_label_fmt='%b %d'`:

```python
# BEFORE
widgets, widget_errors = _load_widgets(
    'week', chart_label_fmt='%b %d', include_recent=True,
)
```

`_chart_series` only falls back to `'%a'` (abbreviated weekday) when `label_fmt is None`. Receiving `'%b %d'` as-is produced labels like `"Jan 05"`, `"Jan 06"` on the initial page render, while a subsequent AJAX period-switch (which calls `_chart_series` without a `label_fmt` override) produced the correct `"Sun"`, `"Mon"` labels. The two code paths showed different label formats for the same week period.

### What was already correct (not changed)

- `_period_start('week', today)` — already computed `today - timedelta(days=today.isoweekday() % 7)`, which correctly returns the Sunday of the current calendar week for every day of the week (see correctness table in the review document).
- `TruncDate` + `USE_TZ=True` + `TIME_ZONE='Asia/Manila'` — Django already grouped orders by Asia/Manila calendar date. No manual timezone conversion was needed.
- The ORM filter `created_at__date__gte=start` — functionally correct for fetching data (the added `__lte` guard is defensive, not a bug fix).

---

## 2. Files Changed — Before / After

### `apps/dashboard/views.py`

**Change A — `_chart_series`: introduce `end` variable and fix loop bound**

```python
# BEFORE
day_sales = (
    Order.objects.filter(is_paid=True, status='completed', created_at__date__gte=start)
    .annotate(day=TruncDate('created_at'))
    .values('day')
    .annotate(total=Sum('total'))
)
...
for i in range((today - start).days + 1):   # ← stopped at today
    day = start + timedelta(days=i)
    labels.append(day.strftime(label_fmt))
    data.append(float(sales_by_day.get(day, 0) or 0))
```

```python
# AFTER
if period == 'week':
    end = start + timedelta(days=6)   # always Saturday (Sun + 6 = Sat)
else:
    end = today                        # month: up to today only

day_sales = (
    Order.objects.filter(
        is_paid=True, status='completed',
        created_at__date__gte=start,
        created_at__date__lte=end,       # ← explicit upper bound
    )
    .annotate(day=TruncDate('created_at'))
    .values('day')
    .annotate(total=Sum('total'))
)
...
for i in range((end - start).days + 1):   # ← always 7 for week (0..6)
    day = start + timedelta(days=i)
    labels.append(day.strftime(label_fmt))
    data.append(float(sales_by_day.get(day, 0) or 0))
```

**Change B — `dashboard_index`: fix label format for initial week render**

```python
# BEFORE
widgets, widget_errors = _load_widgets(
    'week', chart_label_fmt='%b %d', include_recent=True,
)

# AFTER
widgets, widget_errors = _load_widgets(
    'week', chart_label_fmt=None, include_recent=True,
)
```

Passing `None` lets `_chart_series` select `'%a'` for week (→ "Sun", "Mon" …) and `'%b %d'` for month (→ "Jan 05" …), unifying the initial page render with the AJAX refresh path.

### `apps/dashboard/tests.py`

**Change A — `test_chart_data_covers_last_seven_days`: assert fixed Sun–Sat contract**

```python
# BEFORE — iterated today back 6 days (rolling window assumption)
expected = []
for i in range(6, -1, -1):
    day = self.today - timedelta(days=i)
    total = Order.objects.filter(
        is_paid=True, created_at__date=day,        # missing status='completed'
    ).aggregate(t=Sum('total'))['t'] or 0
    expected.append(float(total))

# AFTER — iterates forward from Sunday of the current week
self.assertEqual(labels, ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'])
week_start = self.today - timedelta(days=self.today.isoweekday() % 7)
expected = []
for i in range(7):
    day = week_start + timedelta(days=i)           # Sun=0 … Sat=6
    total = Order.objects.filter(
        is_paid=True, status='completed', created_at__date=day,
    ).aggregate(t=Sum('total'))['t'] or 0
    expected.append(float(total))
```

**Change B — `test_summary_matches_page_statistics`: locate today's sales by day-of-week index**

```python
# BEFORE
self.assertEqual(data['chart_data'][-1], 100.0)  # assumed today was last position

# AFTER
today_index = timezone.localdate().isoweekday() % 7  # Sun=0 … Sat=6
self.assertEqual(data['chart_data'][today_index], 100.0)
```

---

## 3. How the Sunday–Saturday Range Is Calculated

The calculation is split across two functions, both in `apps/dashboard/views.py`:

### Step 1 — Find Sunday (`_period_start`)

```python
def _period_start(period, today):
    if period == 'month':
        return today.replace(day=1)
    # week: Sunday-start calendar week
    return today - timedelta(days=today.isoweekday() % 7)
```

`isoweekday()` returns 1 (Mon) through 7 (Sun). The expression `isoweekday() % 7` maps:

| Day | `isoweekday()` | `% 7` | `today − n` |
|-----|---------------|-------|-------------|
| Sun | 7 | 0 | today itself (Sunday) |
| Mon | 1 | 1 | yesterday (Sunday) |
| Tue | 2 | 2 | 2 days ago (Sunday) |
| Wed | 3 | 3 | 3 days ago (Sunday) |
| Thu | 4 | 4 | 4 days ago (Sunday) |
| Fri | 5 | 5 | 5 days ago (Sunday) |
| Sat | 6 | 6 | 6 days ago (Sunday) |

Every day of the week resolves to the preceding (or same-day) Sunday.

### Step 2 — Pin Saturday as `end` (`_chart_series`)

```python
if period == 'week':
    end = start + timedelta(days=6)   # Sun + 6 days = Sat
else:
    end = today
```

`start` is always a Sunday; `start + 6` is always the following Saturday.

### Step 3 — Generate exactly 7 labels and 7 data points

```python
for i in range((end - start).days + 1):   # range(6 + 1) = range(7) = 0..6
    day = start + timedelta(days=i)
    labels.append(day.strftime('%a'))      # 'Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'
    data.append(float(sales_by_day.get(day, 0) or 0))
```

`(end − start).days = 6`, so `range(7)` always produces indices 0 through 6, one for each day of the week, regardless of which day of the week the page is viewed.

All date math uses `timezone.localdate()`, which returns the current date in Asia/Manila time (`USE_TZ=True`, `TIME_ZONE='Asia/Manila'` in settings). No manual timezone conversion is needed.

---

## 4. How Zero-Sales Days Are Handled

The ORM query returns only days that have at least one matching order:

```python
day_sales = (
    Order.objects.filter(
        is_paid=True, status='completed',
        created_at__date__gte=start,
        created_at__date__lte=end,
    )
    .annotate(day=TruncDate('created_at'))
    .values('day')
    .annotate(total=Sum('total'))
)
sales_by_day = {row['day']: row['total'] for row in day_sales}
```

Days with no matching orders are simply absent from `sales_by_day`. The loop handles this with a `dict.get` default:

```python
data.append(float(sales_by_day.get(day, 0) or 0))
```

- `sales_by_day.get(day, 0)` — returns `0` (integer) if the day is not in the dict (no sales).
- `or 0` — guards against a `None` aggregate result (Django returns `None` not `0` when `Sum` finds no rows, but since this is a per-day bucket from an `.annotate`, it would be absent from the dict rather than `None`; the guard is defensive).
- `float(...)` — converts `Decimal` or `int` to a JSON-serializable float.

The result is that every day in the Sun–Sat window always has a numeric value (≥ 0.0), and days with no completed paid orders always produce exactly `0.0`.

---

## 5. What Was NOT Changed

The following were inspected and confirmed untouched:

| Component | Status |
|---|---|
| `_sales_stats()` — daily/weekly/monthly stat tiles | **Not changed** |
| `_period_start()` — Sunday-start calculation | **Not changed** (already correct) |
| `_top_products()` — Top Products widget | **Not changed** |
| `_low_stock()` — Low Stock widget | **Not changed** |
| `_recent_orders()` — Recent Orders widget | **Not changed** |
| `_status_counts()` — pending/preparing badges | **Not changed** |
| `dashboard_summary` view (realtime endpoint) | **Not changed** |
| `chart_data` view (AJAX endpoint) | **Not changed** |
| Sales Reports (`apps/reports/`) | **Not changed** |
| Order / payment models and logic | **Not changed** |
| Database models and migrations | **Not changed** |
| Chart type (bar), tooltips, Chart.js config | **Not changed** |
| Month period behavior | **Not changed** (`end = today` path is identical to before) |
| Week/Month selector UI | **Not changed** |
| Realtime SSE / WebSocket logic | **Not changed** |

The only changed code paths are:
1. The `if period == 'week': end = ...` block inside `_chart_series` (new 4 lines).
2. The `created_at__date__lte=end` filter argument (1 line).
3. `range((end - start)...` instead of `range((today - start)...` (1 line).
4. `chart_label_fmt=None` instead of `chart_label_fmt='%b %d'` in `dashboard_index` (1 word).
5. Two updated test assertions in `tests.py` (to match the corrected contract).

Sales calculations (totals, subtotals, packaging fees, GCash payments) were not touched.

---

## 6. Django System Check Result

```
$ py manage.py check
System check identified no issues (0 silenced).
```

One `UserWarning` about `SECRET_KEY` was emitted (expected in a dev environment without a `.env` file); it does not represent a system check issue.

---

## 7. Issues and Surprises Discovered During Implementation

### 7.1 Initial-render label mismatch (secondary bug)

The `dashboard_index` call site was passing `chart_label_fmt='%b %d'` for the initial page render. This was a latent bug: the first paint would show labels like `"Jan 05"`, `"Jan 06"` while a subsequent AJAX period-switch would show `"Sun"`, `"Mon"`. The fix (`None` → `_chart_series` picks `'%a'`) corrects both paths in one change.

### 7.2 Existing test assumed a rolling window

`test_chart_data_covers_last_seven_days` iterated `range(6, -1, -1)` — today back six days — and also omitted `status='completed'` from its reference query. Both assumptions were wrong under the new fixed-window contract. The test was updated to iterate forward from Sunday and to match the view's filters exactly.

`test_summary_matches_page_statistics` assumed today's sales would always be the last element in `chart_data[-1]`. Under the fixed Sun–Sat window, today's position is `isoweekday() % 7` (0 for Sunday, 1 for Monday, …, 6 for Saturday). This was corrected.

### 7.3 Pre-existing Top Products / chart window asymmetry (not introduced here)

`_top_products` filters `order__created_at__date__gte=start` with no upper bound, so it covers Sun–today rather than Sun–Sat. With the chart now pinned to Sat, the two widgets reflect different windows on any day that is not Saturday. This asymmetry existed before the fix (it was documented in the original code's docstring: "Sunday of the current calendar week … today") and was not introduced by this change. It is noted in the review findings but was out of scope for this task.

### 7.4 Pre-existing fragile test (`test_summary_month_period_returns_thirty_days`)

This test hardcodes `len(data['chart_labels']) == 30`, but `_chart_series` for month generates `(today − month_start).days + 1` points, which ranges from 1 (1st of the month) to 31 (31st of a long month). This is a pre-existing fragility not introduced by this PR. It was identified in the review and noted for future attention.

---

## Summary

The weekly Sales Overview chart previously showed only 1–7 bars depending on which day of the week the page was viewed, because the loop stopped at `today` instead of the end of the week (Saturday). The fix pins the right-hand boundary to `start + timedelta(days=6)` (always Saturday), ensuring the chart always emits exactly seven data points labeled `['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']`. A secondary label-format bug on the initial page render was corrected at the same time. No sales calculations, order logic, models, migrations, or other dashboard/report sections were changed.
