# Saif Matrimonial — Part 4 Changes

Everything from `CONTINUE_PART4_PROMPT.md`'s outstanding list, except the
two items that genuinely need a real environment this sandbox doesn't
have (load testing, real-browser visual QA — see bottom).

## 1. Payment waiting-lounge UX (spec Phase 11)

**New**: `/track/<code>` — a no-login JSON status endpoint. Looks up a
`request_code` or `package_code` across `unlock_requests` (single or
package) and `self_registrations`, returns only `{found, kind, status}`
— never any name/phone/amount, so it can't leak private data even
though it needs no login (the code itself is already the customer's
private reference number, same trust level as before).

**New**: `templates/_waiting_lounge.html` — a reusable stepper partial
(✓ Details Submitted → ⏳ Verification in Progress → 🔓 Access Unlocked),
plus `static/js/waiting-lounge.js` which polls `/track/<code>` every 4
seconds and updates the stepper live. On approval: "Alhamdulillah! Your
payment has been confirmed." On rejection: a clear message to contact
support via WhatsApp with the reference code. Polling stops as soon as
a terminal state (approved/rejected) is reached.

**Wired into**: `request_submitted.html` (single unlock), `package_submitted.html`
(package purchase), and `registration_submitted.html` (₹11 registration) —
all three now show the live stepper instead of a static "come back later" page.

**Verified**: full `app.test_client()` run — submitted a real registration
with a payment-proof image, confirmed the stepper markup renders with the
correct `data-track-code`, hit `/track/<code>` and got `status: pending`,
then approved it as admin and hit `/track/<code>` again and got
`status: approved`. Also checked malformed/oversized codes correctly
404, and an unknown-but-valid-shaped code returns `{"found": false}`
rather than erroring.

## 2. Social media links (spec Phase 15)

New settings: `instagram_url`, `facebook_url`, `youtube_url`,
`telegram_url` — editable from Admin → Website Settings → Social Media.
Each is sanitized through a new `_sanitize_social_url()` (only `http://`
or `https://` accepted, anything else — including a `javascript:` link —
is silently dropped) before being saved, since these render straight
into an `href` and an admin-only input is still worth defending in
depth. Empty = icon hidden. Displayed in the footer as small circular
icon buttons (📷 📘 ▶️ ✈️ — no external icon-font dependency added).

## 3. Real admin notification feed (spec Phase 36)

**New table** `admin_events` — genuinely distinct from the pre-existing
`notifications` table (which is an admin-authored broadcast shown TO
visitors, and stays exactly as it was — just relabeled "Site
Announcements" on the dashboard so the two aren't confused with each
other).

**New** `create_admin_event()` helper, called from all 6 places
`notify_admin()` already fires: new payment, new package purchase, new
self-registration, new Help Shadi request, new photo request, new shop
order. Each event stores a type, title, short body, related code, and a
direct link to the relevant admin page.

**New** `/admin/events` page — chronological feed with type icons,
read/unread weight, and a "View" link per item. Viewing the feed marks
everything currently listed as read (standard inbox behavior).

**Dashboard integration**: a new "🔔 Notifications" button with a live
unread-count badge, wired into the existing `/admin/api/pending-count`
polling (`static/js/admin-notify.js` already polled this every 15s for
the pending-tasks badge — extended it to also update the events badge,
no new polling loop needed).

**Verified**: submitted a Help Shadi request, confirmed exactly one
`admin_events` row was created with the right type/title and `is_read=0`;
confirmed `/admin/api/pending-count` reported `unread_events: 1`; loaded
`/admin/events` and confirmed the submission appeared; confirmed
`unread_events` dropped to `0` immediately after.

## 4. Explicit SQLite lock-retry (spec Phase 22)

**New** `db_commit_retry(db)` — wraps `db.commit()` with up to 3 retries
and short exponential backoff, but *only* retries on an actual "database
is locked" `OperationalError` (anything else re-raises immediately).
This sits behind WAL mode + `busy_timeout=5000` (Part 1) as a thin
backstop, not the primary defense — SQLite itself already waits and
retries internally for up to 5 seconds before ever raising this error.

**All 55 existing `db.commit()` call sites** were mechanically replaced
with `db_commit_retry(db)` — safe because every one of them used the
exact same `db` variable name and calling convention, confirmed by grep
before doing the replace, and the function signature is a drop-in
replacement (same behavior on the non-error path).

**Verified**: `python3 -m py_compile app.py` after the replace, plus the
full smoke-test suite below still passes — every route that writes to
the database (registration, unlock, Help Shadi, admin actions, backup
logging) was exercised and worked identically to before.

## 5. Missing 429/503 error pages (spec Phase 26)

Added `@app.errorhandler(429)` and `@app.errorhandler(503)`, both using
the existing branded `error.html` template — same pattern as the
already-existing 404/403/413/400/500 handlers. Note: nothing in the app
currently *raises* a 429 or 503 itself (the rate limiters use
flash-and-re-render instead, which is better UX — it preserves what the
customer already typed instead of dropping them on a blank error page),
so this doesn't change any existing behavior; it's a correctness/
completeness fix in case a future change, proxy, or dependency ever
raises either status.

## 6. Lazy-loading images (spec Phase 24)

Added `loading="lazy" decoding="async"` to below-the-fold images: the
homepage's testimonial photos, second/third banner slots, and the whole
profile-card grid (the highest-impact one — this loop can render dozens
of images per page load); the "you may also like" recommended-profiles
grid on `profile_preview.html`; shop product gallery thumbnails; and
shop cart line-item thumbnails. Left eager (no `lazy`) on purpose:
the site logo, the very first/top banner slot, the main photo on a
profile's own page, the main product image on its own page, and every
UPI QR code — all of these are either above-the-fold or a single small
image where lazy-loading has no benefit and only risks a visible pop-in.
(`_product_card.html` already had `loading="lazy"` from Part 1/2 —
left untouched.)

**Note on WebP output**: still JPEG re-encode on upload, unchanged from
before. This was flagged as a minor deviation, not a bug — JPEG at
quality 85 (the current setting) is a reasonable, defensible choice.
Left as-is rather than risking a broader re-encoding change without a
specific ask to do so.

## 7. Backups (spec Phase 30)

**New in-app route**: Admin → Backup (`/admin/backup`) — a page with a
"Download Full Backup Now" button. Hitting it (`/admin/backup/download`)
builds a zip on the fly containing a consistent, WAL-safe snapshot of
`matrimonial.db` (via SQLite's online `.backup()` API — safe to run
while the live site is actively writing, no downtime, no locking) plus
every private/uploaded folder (profile originals, payment proofs, Help
Shadi documents, preview images, branding, banners), and streams it
straight to the admin's device. This is the primary, phone-friendly
backup path — no shell/cron access needed.

**New standalone script**: `backup.py` at the project root, for anyone
who *does* have server/cron access and wants this automated on a
schedule. Same WAL-safe snapshot approach, writes timestamped zips into
`DATA_DIR/backups/`, and prunes down to the most recent N (default 14)
so it never silently fills the disk — while never leaving zero backups
on disk at any point (a new one is always written before old ones are
pruned).

**Restore procedure** is documented in both places (the in-app
`/admin/backup` page and `backup.py`'s docstring): unzip, stop the app,
replace `matrimonial.db` and the storage folders, restart, check
`/healthz`. The in-app page also explicitly tells the admin to actually
test a restore at least once, and to treat this as step 1 (download the
zip) not the whole plan — a copy that only ever lives on the same disk
as the live database doesn't protect against that disk being wiped,
which is exactly the open risk Part 1 flagged and this project has never
confirmed is or isn't the case on the current Render setup.

**Verified**: logged in as admin, hit `/admin/backup` (200) and
`/admin/backup/download` (200, `Content-Type: application/zip`),
unzipped the response in-memory and confirmed `matrimonial.db` is
present and the archive is well-formed. Confirmed both routes 302-redirect
to login when accessed without a session (same `@admin_required` guard
as every other admin route).

## Files touched (Part 4)

| File | Change |
|---|---|
| `app.py` | `/track/<code>`, `admin_events` table + `create_admin_event()`, 6 call sites hooked in, `/admin/events`, `db_commit_retry()` + all 55 commits switched to it, 429/503 handlers, `/admin/backup` + `/admin/backup/download`, social-URL settings + sanitizer, `zipfile` import |
| `templates/_waiting_lounge.html` | **New** — reusable stepper partial |
| `templates/request_submitted.html`, `package_submitted.html`, `registration_submitted.html` | Waiting-lounge stepper included |
| `templates/admin_events.html` | **New** — notification feed page |
| `templates/admin_backup.html` | **New** — backup page + documentation |
| `templates/admin_dashboard.html` | New Notifications + Backup buttons, "Notifications" renamed to "Site Announcements" to avoid confusion with the new feed |
| `templates/admin_settings.html` | New Social Media section |
| `templates/base.html` | Footer social icons |
| `templates/index.html`, `profile_preview.html`, `shop_product.html`, `shop_cart.html` | `loading="lazy"` on below-the-fold images |
| `static/js/waiting-lounge.js` | **New** |
| `static/js/admin-notify.js` | Extended to update the new events badge |
| `static/css/style.css` | Stepper UI, footer social icons |
| `backup.py` | **New** — standalone CLI backup script |

No table/column removed, no route renamed, no destructive changes.

## What's still genuinely outstanding

Only the two items that need a real environment, unchanged from before:

- **Load testing (spec Phase 38)** — never run. Needs a live running
  instance and a load-testing tool (`locust`, `ab`, etc.) hitting it,
  which this sandboxed, no-live-server environment can't do.
- **Real-browser visual QA (spec Phase 39)** — never done. Every UI
  change across all 4 parts has been verified by reading CSS/markup and
  confirming it reuses existing classes/patterns, plus functional
  `app.test_client()` tests — nobody has actually looked at any of it in
  a real browser, on mobile or desktop.

Also worth a mention if picked up later, low priority: ~20 low-opacity
ambient box-shadows elsewhere in `style.css` still carry a hardcoded
green tint (separate from the header/footer/nav bug fixed in Part 3,
which was the visible one). Barely noticeable, not urgent.
