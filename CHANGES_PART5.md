# Saif Matrimonial — Part 5 Changes

Scope for this part came directly from the owner: (1) shop order tracking
by Order ID, (2) invoice/receipt PDF download, (3) the admin dashboard
was "confusing and messy — khichdi, everything dumped in one place."
Audited the full v3 codebase (app.py, all 61 templates, style.css, the
four prior CHANGES files, CONTINUE_PART5_PROMPT.md) before touching
anything. Confirmed: every one of the original 40-phase spec's features
is already built and verified in Parts 1–4 — nothing there was redone.

## 1. Track Order by Order ID (new — this was genuinely missing)

Previously `/shop/order/<order_code>` existed and worked, but a customer
could only reach it via the exact link shown right after checkout — if
they closed that tab or switched devices, there was no way back in.

**New route**: `GET/POST /track-order` (`app.py`) — a public lookup form.
Customer types their Order ID (e.g. `ORD4F2A9C1B`), it's checked against
`shop_orders.order_code` (case-insensitive), and on a match redirects to
the existing `/shop/order/<code>` status page. Wrong/unknown code just
re-shows the form with a flash error — never a raw 404 or stack trace.

**New template**: `templates/track_order.html` — matches existing card/
form styling, includes the CSRF token (required by the app's existing
`enforce_csrf` before_request hook — confirmed by testing that a POST
without it correctly gets rejected).

**Wired in**: `templates/base.html` — added a "Track Order" link to the
desktop nav, mobile nav, and footer "For Users" column. `templates/
shop_order_status.html` — added a highlighted "📌 Save this Order ID"
box right after checkout linking to the new page, since that's the
one moment the customer is guaranteed to be looking at the code.

## 2. Invoice / Receipt PDF download (new — this was genuinely missing)

`reportlab` was already a dependency and already used for profile
biodata PDFs (`generate_profile_pdf_bytes`) — reused that exact pattern.

**New function**: `generate_shop_invoice_pdf_bytes()` (`app.py`, next to
the existing profile-PDF helpers) — builds a clean one-page PDF with
order code, date, customer/delivery details, itemized table, total, and
a footer. Labels itself **"ORDER RECEIPT (Payment Pending Verification)"**
with an explicit "this is not confirmation of payment" notice when
`payment_status != 'paid'`, and **"INVOICE"** once payment is confirmed —
never overstates what's actually been verified.

**New route**: `GET /shop/order/<order_code>/invoice` — same trust model
as the existing status page (gated by knowledge of the order_code, which
is already the customer's private reference number; nothing new exposed).

**Wired in**: a "📄 Download Invoice" / "Download Receipt (Payment
Pending)" button added next to "Continue Shopping" on the order status
page, label changes automatically based on `payment_status`.

## 3. Admin Dashboard redesign (owner: "khichdi", "boring", "hard to find things")

**Before**: one flat 13-tile stat grid, then 20 buttons in a single
wrapped row with no grouping — Website Settings sat next to Logout sat
next to Shop Orders sat next to Backup, in whatever order they'd been
added over four parts.

**After** (`templates/admin_dashboard.html` — full rewrite, same route/
view function, only the template and the `stats` dict passed to it
changed):

- **Priority alert banner** at the top — red/amber if anything needs
  action, with tappable chips straight to each pending queue (unlock
  requests, registrations, photo requests, shop orders, Help Shadi);
  green "You're all caught up" when nothing's pending. Driven by a new
  `stats.needs_attention` total computed in the view.
- **Grouped sections**, most time-sensitive first: Payments & Requests →
  Profiles → Islamic Shop → Branding & Content → System → Recent
  Pending Requests → Recent Admin Activity. Each section has its own
  heading with an icon, so the admin can jump straight to the area they
  need instead of scanning one giant list.
- Each item is now an icon tile with its own pending-count badge
  (previously badges only existed on 3 of the 20 buttons; shop orders
  and unlock requests had none).
- **Shop orders were missing from the dashboard stats entirely** —
  fixed: added `stats.shop_orders_pending` / `stats.shop_orders_total`
  (new `SELECT COUNT(*)` queries in `admin_dashboard()`, additive only).

**New CSS** (`static/css/style.css`, appended, nothing existing removed
or renamed): `.admin-alert` (+ `.is-warn`/`.is-ok` variants and chips),
`.admin-section` / `.admin-section-head`, `.admin-tile-grid` / `.admin-tile`
(+ danger/logout variants). Reuses the existing color variables
(`--emerald`, `--gold`, `--ivory` etc.) and the existing `.badge-pending`
class — no new color system, no new JS, no new libraries.

## Database changes

**None.** No schema changes, no migrations, no new tables/columns.
Part 5 only added two new `SELECT COUNT(*)` read queries against the
existing `shop_orders` table.

## New dependencies

**None.** Everything above uses `reportlab` (already in
`requirements.txt` since Part 1) and Flask/Jinja already in use
throughout.

## Files touched

- `app.py` — added `generate_shop_invoice_pdf_bytes()`, three new routes
  (`shop_order_invoice`, `track_order`), two new stats keys +
  `needs_attention` in `admin_dashboard()`. No existing route renamed,
  removed, or behaviourally changed.
- `templates/admin_dashboard.html` — full rewrite (layout only; every
  link still points at the same existing endpoints).
- `templates/shop_order_status.html` — added the save-your-code notice
  and the invoice/receipt download button.
- `templates/track_order.html` — new file.
- `templates/base.html` — added 3 "Track Order" nav/footer links.
- `static/css/style.css` — appended new admin-dashboard classes only.

## Verified

Ran in an isolated copy of the project (`DATA_DIR` pointed at a scratch
folder so the real `matrimonial.db` was never touched) using
`app.test_client()`:

- `python3 -m py_compile app.py` — clean.
- `GET /track-order` → 200; `POST /track-order` with a bad code → 200
  + flash error (not a 404); with a real order code (including
  lowercase input) → 302 redirect to the correct status page.
- Confirmed the CSRF hook correctly rejects a POST with no token
  (protection is active), then added the hidden field and confirmed a
  valid submission goes through.
- Inserted a test `shop_orders` row with `payment_status='pending'`:
  `GET /shop/order/<code>/invoice` → 200, `application/pdf`, correct
  "ORDER RECEIPT (Payment Pending Verification)" wording. Updated it to
  `payment_status='paid'`: same route → "INVOICE" wording. A nonexistent
  code → 404.
- Logged in as admin and loaded `/admin`: confirmed the "all caught up"
  banner when nothing is pending, then inserted a pending shop order and
  confirmed the red banner + correct chip count appeared.
- Crawled every public page and every existing admin page
  (profiles, requests, registrations, photo requests, shop
  orders/products/categories, settings, appearance, banners,
  notifications, events, backup, agents, testimonials, Help Shadi) —
  all still return 200, nothing broke.

## Not done / still outstanding (same two items Part 4 already flagged — unchanged)

1. **Real load/concurrency testing** (spec Phase 38) — still not
   executed. This sandbox has no way to hit a real running instance
   with concurrent traffic (`locust`/`ab` need a live server + network
   access this environment doesn't have). WAL mode, `busy_timeout`, and
   retry-on-lock are in place, but that's still a prediction, not a
   measured fact.
2. **Real-browser visual QA** (spec Phase 39) — the new dashboard/
   invoice/track-order UI has been verified by reading the CSS/markup
   and by functional `test_client()` checks that the right elements
   render, but nobody has looked at it in an actual browser on real
   screen sizes yet. If you have real browser access, specifically
   check: the new dashboard tiles/banner at 360–414px, the invoice PDF
   layout, and the track-order form.

If something new comes up next, treat it as a fresh task: inspect the
current code first (state may have changed), keep the same
non-negotiable rules from `CONTINUE_PART5_PROMPT.md`, and update this
file (add `CHANGES_PART6.md`) rather than editing this one.
