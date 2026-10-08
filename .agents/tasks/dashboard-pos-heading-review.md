# Dashboard and POS page-heading consistency

A six-line change adds the "Dashboard" main page heading to dashboard/index.html. The POS template already had no visible page heading, so the design distinction—management pages with headings, POS as a heading-free workspace—is now complete.

**Watch for:** nothing blocking. The Dashboard heading uses the existing `.page-header` / `.page-title` system, placement is correct (before `#dashboard-content`, not duplicated), and POS is already heading-free. **(confirmed)**

**Verdict**: APPROVED

---

## High-level view

The Dashboard heading was added as a `.page-header > h1.page-title` before the `#dashboard-content` wrapper, matching the markup pattern used by Inventory, Categories, Staff Accounts, and other management pages. The heading sits outside `#dashboard-content`, so the dashboard's loading-skeleton reveal logic is unaffected.

The POS template's `{% block content %}` opens directly with `<div class="pos-layout" style="margin:-28px">` — no heading, no `.page-header`. The `{% block page_title %}POS Terminal{% endblock %}` goes only to the browser `<title>` tag; base_admin.html never renders it as visible on-page text.

No changes were made to pos.html, base_admin.html, base.html, or any CSS file.

<details>
<summary>Issues (0)</summary>

No blocking issues.

</details>

<details>
<summary>Details</summary>

## POS tab title vs. visible heading

One nuance worth noting: pos.html still carries `{% block page_title %}POS Terminal{% endblock %}`. Because base_admin.html feeds `page_title` only into the `<title>` tag (via base.html), this is browser chrome only — it never appears as a visible heading in the page content. Confirmed by reading base_admin.html in full: the `.page-content` div contains only `{% block content %}`, with no shared page-header injection. The design intent — POS workspace starts immediately, no on-page title — is fully preserved.

</details>

---

<details>
<summary>File map</summary>

**templates/dashboard/index.html** — added `.page-header > h1.page-title` (6 lines) before `#dashboard-content`

Full diff: `git diff HEAD~1 HEAD -- templates/dashboard/index.html`

</details>
