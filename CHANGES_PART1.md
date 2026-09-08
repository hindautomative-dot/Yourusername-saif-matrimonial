# Saif Matrimonial — Part 1 Changes

Generated against the `v3` zip you uploaded. If you've changed anything
in your live repo since exporting that zip, diff these 3 files against
your current version before overwriting — don't blindly replace.

## Audit summary (Phase 0)

This codebase is **far more mature than the spec assumed** — most of the
40-phase request is already built and working:

**Already implemented (verified by reading the code, not assumed):**
- OTP-based "My Requests" access, agent portal with its own OTP+code login
- DB-backed Website Settings (branding, colors, fonts, hero text, logo,
  favicon, QR, coins config) — `/admin/settings`, `/admin/settings/appearance`
- Banners, testimonials ("success stories"), shop (categories/products/
  variants/cart/checkout/orders), Coins wallet (earn + redeem)
- CSRF protection, security headers (`X-Frame-Options`, CSP)
- Rate limiting on admin login, OTP request/verify, agent login (a
  `RateLimiter` class already existed — see below)
- Photo privacy: private storage dir, per-viewer watermarking, consent
  declaration before unlock, verification badges
- Recommendations ("similar profiles" via hobbies/city), compatibility
  score, pagination, DB indexes on the filtered columns
- FAQ mini-assistant (client-side keyword matching, `static/js/main.js`)
- SEO (`robots.txt`, `sitemap.xml`, canonical URLs)
- `DATA_DIR` env var already routes the DB + private uploads to a
  mountable persistent volume path — **you still need to verify Render
  actually has a persistent disk mounted there**; the code is ready for
  it either way (see "Still needs your attention" below)
- Admin activity log, admin notifications, PDF biodata export, Excel export

**Genuine gaps found (and fixed in this part — see below):**
1. No SQLite WAL/busy-timeout tuning (Phase 22)
2. No `/healthz` endpoint (Phase 27)
3. Registration and unlock/payment forms had no rate limiting, even
   though the `RateLimiter` class already existed and was used elsewhere
   (Phase 25)
4. No first-visit welcome overlay (Phase 3)

**Not started yet (Part 2 — see the continuation prompt):**
- Help Shadi (Phase 16) — entirely new feature, not present at all
- Final security/load-test audit passes (Phases 37-38)
- Visual QA pass (Phase 39) — the design system already matches the
  spec's color/typography direction closely, so this is more of a
  polish/verification pass than new work

## Files changed

| File | Change |
|---|---|
| `app.py` | +26 lines, 0 removed. See detail below. |
| `templates/base.html` | +11 lines, 0 removed. Welcome overlay markup + script tag. |
| `static/css/style.css` | +67 lines appended at the end. Welcome overlay styles only. |
| `static/js/welcome.js` | **New file.** Welcome overlay show/dismiss logic. |

### `app.py` — exactly what changed

1. **`get_db()`** — added 3 PRAGMA statements (`journal_mode=WAL`,
   `busy_timeout=5000`, `synchronous=NORMAL`) and a 10s connect timeout.
   Standard, low-risk SQLite concurrency tuning — lets reads happen while
   a write is in progress instead of blocking, and makes SQLite itself
   wait/retry for 5s on a locked DB instead of raising immediately.
2. **New route `/healthz`** — runs `SELECT 1`, returns
   `{"status": "ok"}` (200) or `{"status": "error", ...}` (503). For
   Render's health check / any uptime monitor.
3. **Two new `RateLimiter` instances** (`registration_limiter`,
   `unlock_submit_limiter`) using the *same class already in the file* —
   nothing new introduced, just applied to two forms that weren't
   covered yet.
4. **`register_yourself()`** and **`unlock()`** — each now checks
   `is_locked(ip)` at the top of the POST handler and calls
   `register_attempt(ip)`, mirroring the exact pattern already used in
   `admin_login()`.

Nothing was removed, renamed, or restructured. No database schema
changed (no migration needed for this part).

## Verification performed

I don't have a way to run your actual Render deployment, but I did run
real checks against this code, not just read it:

- `python3 -m py_compile app.py` → **no syntax errors**
- Jinja2 template parser against `base.html` and every template that
  extends it → **no template syntax errors**
- Actually imported the app and ran it against a throwaway SQLite DB in
  an isolated sandbox (`flask.testing`), then:
  - `GET /healthz` → `200 {"status": "ok"}`
  - `GET /` (homepage, now includes the welcome overlay markup) → `200`
  - `GET /register-yourself` → `200`
  - `POST /register-yourself` × 7 in a row → all handled without a
    crash (each returned 400 from your existing CSRF protection, which
    is correct behavior for a request with no CSRF token — confirms
    CSRF wasn't broken by this change, though it means I couldn't
    observe the lockout message itself without also simulating a valid
    CSRF token)

I could not test: the actual visual appearance of the welcome popup (no
browser here), real concurrent-write behavior under load, or anything
that depends on Render's specific environment (persistent disk,
real `DATA_DIR`, real SMS/OTP provider).

## Still needs your attention (not something I can verify remotely)

1. **Confirm Render has a persistent disk mounted at your `DATA_DIR`
   path.** If it doesn't, your SQLite DB and uploaded photos/payment
   proofs are wiped on every redeploy — this is a Phase 29 risk the
   spec explicitly asked to flag, and I can't check your Render
   dashboard from here.
2. **Look at the welcome popup in a real browser** before trusting it —
   I verified it renders without a server error and reads reasonably in
   the HTML/CSS, but I have no way to screenshot it here.
3. Deploy this to a staging URL (or Render's preview) and click through
   registration + unlock once for real before it reaches production
   customers.
