# Weekly Sales Overview: Sun–Sat fixed-window chart

The weekly Sales Overview chart previously showed a rolling window from Sunday of the current week through *today*, meaning early in the week the chart had fewer than 7 bars and future days were absent. This fix pins the right-hand boundary to always be Saturday (`start + 6 days`), giving a constant 7-bar chart regardless of which day of the week the page is viewed. The only files changed are `apps/dashboard/views.py` and `apps/dashboard/tests.py`.

Watch for: (1) **confirmed** — `_top_products` still uses an open-ended `created_at__date__gte=start` with no upper bound, so it counts sales Sun–today while the chart covers Sun–Sat; the two widgets now have different windows when today < Saturday. (2) **confirmed** — `dashboard_index` was passing `chart_label_fmt='%b %d'` to `_load_widgets`, which would have formatted week labels as "Jan 05" instead of the abbreviated day names; the fix corrects this by passing `None` and letting `_chart_series` pick `'%a'` for the week period.

**Verdict**: APPROVED

---

## High-level view

The root cause was that `_chart_series` used `today` as the loop's right-hand boundary. For the week period, `today` could be any day Sunday–Saturday, so the chart had 1–7 bars depending on when you looked. The fix introduces a `end` variable: for `week` it is always `start + timedelta(days=6)` (Saturday), for `month` it remains `today`. The loop bound and the ORM filter upper bound both use `end`, so the chart always emits exactly 7 data points labeled `['Sun','Mon','Tue','Wed','Thu','Fri','Sat']`.

The `_period_start` function and `_sales_stats` were already computing Sunday-start week boundaries correctly using `today.isoweekday() % 7`; those are unchanged. The fix only extends `_chart_series` to use a fixed Saturday endpoint rather than stopping at today.

A secondary fix in `dashboard_index` corrects a format-string bug: the page render was passing `chart_label_fmt='%b %d'` (month-day format) when loading the week chart, which would have produced labels like "Jan 05" instead of "Sun". The fix passes `None` so `_chart_series` selects `'%a'` itself.

The `_top_products` function still filters `created_at__date__gte=start` with no upper bound, so it covers Sunday through today rather than Sunday through Saturday. This is a pre-existing window mismatch that the fix does not address. On most days the discrepancy is minor, but on a Saturday it means Top Products includes the full week while on a Sunday (the first day of the week) it only includes today's orders.

Month behavior, Sales Reports, Top Products query logic, order/payment models, and all other dashboard sections are untouched.

---

<details>
<summary>Issues (2)</summary>

1. **Top Products / chart window mismatch** — `_top_products` uses `created_at__date__gte=start` with no upper bound (covers Sun–today), while the chart now covers Sun–Sat. On any day before Saturday, Top Products and the Sales Overview bars reflect different time windows. Add `order__created_at__date__lte=end` to `_top_products` (passing `end` similarly to `_chart_series`) to align the two widgets. This was not introduced by this PR but the PR's explicit goal of making the chart cover Sun–Sat makes the asymmetry more visible.

2. **`test_summary_month_period_returns_thirty_days` hardcodes 30** — this test (pre-existing, not introduced here) asserts `len(data['chart_labels']) == 30`, but `_chart_series` for `month` generates `(today - month_start).days + 1` points, which varies from 1 (1st of the month) to 31 (31st of a long month). The test only passes when run on the 30th of a month or when the test fixture seeds an order exactly 30 days before `today`. This is a pre-existing fragility, not caused by this PR, but worth noting for the next time it silently flips to a different day count.

</details>

---

<details>
<summary>Details</summary>

### `isoweekday() % 7` correctness across all days

The Sunday-start formula `today - timedelta(days=today.isoweekday() % 7)` is confirmed correct for every day of the week:

| Day | `isoweekday()` | `% 7` | `today - n` |
|-----|---------------|-------|-------------|
| Sun | 7 | 0 | today (Sunday itself) |
| Mon | 1 | 1 | yesterday (Sunday) |
| Tue | 2 | 2 | Sunday |
| Wed | 3 | 3 | Sunday |
| Thu | 4 | 4 | Sunday |
| Fri | 5 | 5 | Sunday |
| Sat | 6 | 6 | Sunday |

`isoweekday()` returns 7 for Sunday (not 0), and `7 % 7 == 0` means Sunday subtracts zero days from itself — exactly right. Every other day subtracts its 1-based ordinal, landing on the preceding Sunday.

### The `chart_label_fmt='%b %d'` bug

Before this commit, `dashboard_index` called:

```python
_load_widgets('week', chart_label_fmt='%b %d', include_recent=True)
```

`_load_widgets` passes `chart_label_fmt` directly to `_chart_series` as `label_fmt`. `_chart_series` only falls back to `'%a'` when `label_fmt is None`, so `'%b %d'` would have been used as-is — producing "Jan 05, Jan 06, …" for the week chart. The fix passes `None`, letting `_chart_series` select `'%a'` (abbreviated weekday). The `chart_data` AJAX endpoint and the `dashboard_summary` endpoint were not affected because they call `_chart_series(period)` and `_load_widgets(period)` without a `label_fmt` override.

### Top Products window asymmetry (confirmed, not introduced here)

```python
# _top_products — unchanged in this PR
return list(
    OrderItem.objects
    .filter(
        order__status='completed',
        order__created_at__date__gte=start,   # no upper bound
    )
    ...
)
```

The chart query now has `created_at__date__lte=end` (Saturday); Top Products does not. The docstring even says "Sunday of the current calendar week … today", confirming the intent was always `today` as the upper bound. With the chart now pinned to Saturday, these two widgets describe different windows for any day that is not Saturday.


</details>

---

<details>
<summary>File map</summary>

| File | What changed |
|------|-------------|
| `apps/dashboard/views.py` | `_chart_series`: added `end` variable (week → Saturday, month → today), updated ORM filter and loop range to use `end`; `dashboard_index`: corrected `chart_label_fmt='%b %d'` → `None` |
| `apps/dashboard/tests.py` | `test_chart_data_covers_last_seven_days`: asserts exact `['Sun'…'Sat']` label order and iterates forward from `week_start`; `test_summary_matches_page_statistics`: locates today's sales by `isoweekday() % 7` index instead of assuming last position |

Full diff: `git diff HEAD~1 HEAD`

</details>
