# Saif Matrimonial — Part 2 Changes (Help Shadi)

Generated against the `v3` zip you uploaded (which already had Part 1
applied — see `CHANGES_PART1.md`). This part adds Help Shadi, the one
genuinely missing feature Part 1's audit identified. Security audit and
mobile-UX checks (items 2–3 of your task) are folded into this report
rather than a separate pass, since Help Shadi was the only new surface
area to check.

## Files changed

| File | Change |
|---|---|
| `app.py` | +~200 lines, 0 removed, 0 renamed. See detail below. |
| `templates/base.html` | +2 lines nav (desktop), +1 line nav (mobile), +1 line footer. "Help Shadi" link added, same style as other static nav items — no existing conditional-nav-from-settings pattern was found, so it's a static link like Home/FAQ/Contact. |
| `templates/admin_dashboard.html` | +2 stat cards, +1 quick-action button, all following the exact existing markup pattern (`stats.reg_pending`-style). |
| `templates/help_shadi.html` | **New.** Public info page — exact copy you specified, "Help Shadi" / "₹5 se bhi shuruaat ho sakti hai." + UPI QR, reusing `qr_image`/`upi_id` (no new settings). |
| `templates/help_shadi_register.html` | **New.** Application form, built on the same card/form CSS classes as `register_yourself.html` (`container-narrow`, `.card.reg-card`, same label/input/textarea markup, same `data-autoresize` photo-resize script). |
| `templates/help_shadi_submitted.html` | **New.** Confirmation page, same shape as `registration_submitted.html`. |
| `templates/admin_help_shadi.html` | **New.** Admin list/review page, built directly on `admin_registrations.html`'s table structure and tab-filter pattern, extended with 5 statuses and a notes field. |

Nothing was removed, renamed, or restructured. No existing route, table,
or column was touched.

## New table

```sql
CREATE TABLE IF NOT EXISTS help_shadi_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guardian_name TEXT NOT NULL,
    candidate_name TEXT NOT NULL,
    contact TEXT NOT NULL,
    address TEXT NOT NULL,
    assistance_category TEXT NOT NULL,
    details TEXT,
    document_filename TEXT,
    status TEXT DEFAULT 'pending',       -- pending/under_review/approved/rejected/completed
    admin_notes TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_help_shadi_status ON help_shadi_requests(status);
```

Added via `CREATE TABLE IF NOT EXISTS` inside the existing `init_db()`,
same style as every other table in the file. No `ALTER TABLE` was
needed since this is a brand-new table, not a column added to an
existing one.

## `app.py` — exactly what changed

1. **New constant** `PRIVATE_HELP_SHADI_DOCS_DIR` — added to the same
   `os.makedirs(...)` loop as the other private storage dirs. Lives
   under `DATA_DIR/storage/private/`, same as payment proofs — never
   under `static/`.
2. **New table + index** in `init_db()` (above).
3. **New rate limiter** `help_shadi_limiter = RateLimiter(max_attempts=5,
   window_seconds=600, lockout_seconds=900)` — same class, same
   parameters as `registration_limiter`, just a separate instance so
   Help Shadi abuse doesn't share a bucket with profile registration.
4. **New function** `save_help_shadi_document()` — mirrors
   `save_payment_proof()` exactly (same `_load_validated_image()` call,
   same JPEG re-encode, same random `secrets.token_hex(16)` filename),
   saving into `PRIVATE_HELP_SHADI_DOCS_DIR` instead. Kept as its own
   function rather than literally reusing `save_payment_proof()` so the
   two can diverge later without coupling payment-proof code to this
   feature.
5. **New public routes:**
   - `GET /help-shadi` — the info page.
   - `GET+POST /help-shadi/register` — the application form. POST path
     mirrors `register_yourself()`: `help_shadi_limiter.is_locked()` /
     `.register_attempt()` at the top (same as `registration_limiter` in
     `register_yourself`), server-side validation of every required
     field (`valid_indian_phone()` reused for the contact number),
     optional document upload via `save_help_shadi_document()`, then an
     `INSERT` with parameterized `?` placeholders and a `notify_admin()`
     call — the same admin-alert helper used by every other
     submission flow (payments, self-registration, packages). This
     project's `notifications` table is a separate thing — an
     admin-authored banner shown *to site visitors* — not an
     admin-alert mechanism, so `notify_admin()` (SMS/email/webhook) was
     the correct helper to reuse, matching how `register_yourself()` and
     `unlock()` already do it.
6. **New admin routes:**
   - `GET /admin/help-shadi` — list with status-tab filter, mirrors
     `admin_registrations()` exactly (same query-param pattern, same
     `status_filter != 'all'` branch).
   - `GET /admin/help-shadi/<id>/document` — `@admin_required`,
     `send_from_directory(PRIVATE_HELP_SHADI_DOCS_DIR, ...)`, same
     `Cache-Control: no-store, private` header as
     `admin_payment_proof()` / `admin_registration_proof()`. 404s if the
     row or file doesn't exist.
   - `POST /admin/help-shadi/<id>/<action>` — `@admin_required`,
     validates `action` against the 5 allowed statuses, updates
     `status` + `admin_notes` + `decided_at`, calls `log_admin_action()`
     (same activity-log helper used everywhere else in the admin panel).
7. **Dashboard/badge integration** — `admin_api_pending_count()` and
   `admin_dashboard()` both now include `help_shadi_pending` /
   `help_shadi_total` counts, following the exact pattern already used
   for `reg_pending` / `photo_req_pending`, so the existing live pending
   badge in the nav picks up Help Shadi automatically with no separate
   polling logic.

## Security audit (Phase 37) — Help Shadi-specific findings

- **IDOR check on existing proof routes** (as you asked me to confirm,
  not assume): `admin_payment_proof`, `admin_shop_payment_proof`,
  `admin_registration_proof` — all three already carry `@admin_required`
  directly above the route function. Confirmed by reading the decorator
  stack, not inferred. Same applies to the two new Help Shadi
  document/admin routes I added.
- **New document route never public**: `/admin/help-shadi/<id>/document`
  is under `/admin/...`, gated by `@admin_required`, and serves from
  `PRIVATE_HELP_SHADI_DOCS_DIR` (outside `static/`) via
  `send_from_directory` — there's no `static/` alias or symlink into
  that directory anywhere in the codebase (verified: only `PREVIEW_DIR`,
  `BRANDING_DIR`, `BANNERS_DIR` live under `static/`).
- **SQL injection**: every new query uses `?` placeholders
  (`db.execute("... WHERE id = ?", (req_id,))` etc.) — no f-string or
  `%`-formatted SQL was introduced anywhere in this part.
- **CSRF**: both new forms (`help_shadi_register.html`'s POST and every
  admin action form in `admin_help_shadi.html`) include
  `{{ csrf_token() }}` — confirmed the existing global CSRF protection
  actually rejects requests without it (see smoke test results below —
  a request with no token got a 400).
- **Rate limiting**: `/help-shadi/register` is now covered by
  `help_shadi_limiter`, same lockout behavior as `/register-yourself`.

## What I verified (and how)

- `python3 -m py_compile app.py` → no syntax errors.
- Jinja2 `Environment(loader=FileSystemLoader("templates"))` +
  `env.get_template(...)` against every new/touched template (`base.html`,
  `help_shadi.html`, `help_shadi_register.html`, `help_shadi_submitted.html`,
  `admin_help_shadi.html`, `admin_dashboard.html`, `register_yourself.html`)
  → no template syntax errors.
- **Actual `app.test_client()` smoke tests** run against a throwaway
  SQLite DB in an isolated sandbox (Flask, Pillow, reportlab, openpyxl
  were all available in this environment):
  - `GET /help-shadi` → 200
  - `GET /help-shadi/register` → 200, CSRF token present in the form
  - `POST /help-shadi/register` with no CSRF token → 400 (confirms CSRF
    protection wasn't bypassed by the new route)
  - `POST /help-shadi/register` with a valid token and all required
    fields → 200, "Application Submitted" page rendered, row correctly
    inserted into `help_shadi_requests` with `status='pending'`
  - `POST /help-shadi/register` with a real in-memory JPEG attached as
    `document` → file saved under `PRIVATE_HELP_SHADI_DOCS_DIR` (outside
    `static/`), `document_filename` correctly stored on the row
  - Admin login → `GET /admin` (dashboard) → 200, new Help Shadi stat
    cards and quick-action link render correctly
  - `GET /admin/help-shadi` → 200, the test submission appears in the
    pending list
  - `POST /admin/help-shadi/<id>/under_review` with notes → 200,
    row's `status` and `admin_notes` updated correctly in the DB
  - `GET /admin/help-shadi/<id>/document` as admin → 200,
    `Content-Type: image/jpeg`
  - `GET /admin/help-shadi/<id>/document` for a row **without** a
    document → 404; for a **nonexistent** id → 404
  - **Logged out**, `GET /admin/help-shadi` and
    `GET /admin/help-shadi/<id>/document` → both 302 redirect to admin
    login (confirms `@admin_required` actually blocks unauthenticated
    access, not just present in source)
  - Regression check on unrelated existing routes after all the above
    (`/`, `/healthz`, `/register-yourself`, `/shop`, `/faq`,
    `/how-it-works`, `/verify-access`) → all still 200, nothing broken

## What I could NOT verify

- **Real visual/mobile appearance** — no browser in this environment. I
  read the CSS classes used (`container-narrow`, `.card.reg-card`,
  `.hobbies-grid`-style label/input structure, `.qr-box`, `.upi-id-box`)
  and reused them exactly as `register_yourself.html` and
  `admin_registrations.html` already do, without introducing any new
  breakpoint or class — but I can't confirm rendering pixel-for-pixel.
- **Real concurrent-write / production load behavior** — same
  limitation Part 1 already noted; nothing in this part changes that.
- **Live SMS/email/webhook delivery from `notify_admin()`** — this
  depends on your `ADMIN_NOTIFY_PHONE` / `ADMIN_NOTIFY_EMAIL` /
  `SMTP_*` / `WHATSAPP_API_*` env vars being set on your actual
  deployment; I confirmed the function is *called* correctly with the
  right subject/message, not that a real SMS/email arrives, since those
  providers aren't reachable from this sandbox.
- **Render's persistent disk mount** — same open item Part 1 flagged;
  `PRIVATE_HELP_SHADI_DOCS_DIR` follows the same `DATA_DIR`-relative
  path as the other private dirs, so it inherits whatever answer you
  get for that.

## From the original 40-phase spec — still not done after this part

- **Phases 37–38 (broader security/load-test audit)**: I covered the
  specific checks you listed (IDOR on proof routes, new-route privacy,
  SQL injection) plus everything Help Shadi touches. I did not re-run a
  full penetration-style pass over the *entire* 4,300+ line app — Part 1
  already covered CSRF/headers/rate-limiting broadly, and a genuine
  load test needs a real environment I don't have here.
- **Phase 39 (visual QA pass)**: still just the CSS-reading-level check
  described above, not a real-browser pass across breakpoints.
- Anything else Part 1's audit already flagged as unstarted and outside
  Help Shadi's scope remains unstarted — Part 1's `CHANGES_PART1.md` is
  still the accurate source for that list.

## Addendum — extended app-wide audit pass (no code changed, read-only)

Went beyond Help Shadi and checked the whole file mechanically. Nothing
below required a code change — all findings are "confirmed safe" or
"pre-existing, flagged for your awareness":

- **IDOR — every admin route**: grepped all 88 `@app.route` definitions.
  All 45 `/admin/...` routes carry `@admin_required` except
  `/admin/login` and `/admin/logout`, which correctly must not (that's
  how you log in/out). No admin route is missing its guard.
- **IDOR — agent portal**: all `/agent/...` routes that should be
  protected carry `@agent_required`; `agent_my_activity` scopes its
  query to `session["agent_id"]` (can't view another agent's log by
  guessing an ID). `agent_profile_view` looks up by `profile_code`
  without an agent-ownership check, but that's correct — profile
  browsing is an agent's actual job, not private-to-one-agent data.
- **"My Requests"**: scoped by `session["verified_phone"]` (OTP-verified
  server-side), not a URL parameter — a user can't view another
  person's unlock history by editing an ID in the URL.
- **SQL injection — every f-string query in the file**: found 6 places
  where SQL is assembled with an f-string (`_ensure_column`'s
  `ALTER TABLE`, and 4 profile-listing routes' dynamic `WHERE` clauses).
  In all 6, the interpolated parts are either hardcoded developer-time
  strings (table/column names) or a `sort_sql` value pulled from a
  hardcoded dict via `.get()` with a safe fallback — every actual user
  value goes through a separate `params` list bound with `?`
  placeholders. No injectable string reaches an f-string anywhere.
- **File uploads — every `request.files.get(...)` call in the app**:
  all image-accepting ones (profile photos, payment proofs, Help Shadi
  documents, banners, testimonials, shop product images, logo/favicon/QR)
  route through the same `_load_validated_image()` (extension whitelist,
  6MB cap, PIL `.verify()`, re-encoded to JPEG — strips any embedded
  payload). The one exception is the custom-font uploader in
  `/admin/settings/appearance`, which validates extension/size but isn't
  an image — this is fine since it's admin-only (behind
  `@admin_required`), not a public upload surface.
- **XSS**: one `|safe` filter exists in the whole template set —
  `hero_heading` on the homepage. Traced its only write path: it's set
  through `/admin/settings` (admin-only, CSRF-protected), never from any
  public form. Not a public XSS vector, but worth knowing it's there if
  an admin account is ever compromised. None of the new Help Shadi
  templates use `|safe` anywhere.
- **Mobile UX / CSS**: every class used in the 4 new Help Shadi templates
  (`container-narrow`, `reg-card`, `qr-box`, `upi-id-box`, `helptext`,
  `checkbox-row`, `btn-block`, `btn-gold`, `stat-card`, `tabs`,
  `table-wrap`, `empty-state`, badge classes) already exists in
  `static/css/style.css` from other pages — confirmed by grep, not
  assumed. No new breakpoint or class was introduced anywhere.
