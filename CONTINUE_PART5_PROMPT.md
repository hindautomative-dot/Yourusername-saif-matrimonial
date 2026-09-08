I'm uploading my live Flask/SQLite matrimonial website ("Saif Matrimonial
Services", zip attached — this is `v3` + Parts 1–4 already applied: see
`CHANGES_PART1.md` through `CHANGES_PART4.md` inside the zip for exactly
what each part did).

This is a LIVE business with real users/profiles/payments. Follow these
non-negotiable rules:

1. Do NOT rebuild from scratch. Do NOT touch/reset the database. Do NOT
   remove or rename any existing route, table, or column.
2. Inspect the actual code before writing anything — don't assume.
3. Every DB change must be additive (new tables/columns only, via
   `_ensure_column()` / `CREATE TABLE IF NOT EXISTS`, matching the
   pattern already used throughout `app.py`).
4. No new heavy dependencies (no Redis/Celery/Kubernetes) unless you can
   prove SQLite + Flask genuinely can't do it.
5. After you're done, verify with `python3 -m py_compile app.py`, a
   Jinja2 template-syntax check on any templates you touched, and an
   actual `app.test_client()` smoke test hitting the new/changed routes.
   Report honestly if you can't run some of these rather than claiming
   untested code works.
6. Don't use the phrase "Shubh Shuruat" anywhere (Part 3 — rejected by
   the owner, Hindu-festival connotation).
7. Keep the Bismillah spelling as `Bismillah hir Rahman nir Raheem` if
   you touch that text (Part 3 — deliberately corrected).

## What's already done (read the four CHANGES files first — don't redo any of it)

Every feature in the original 40-phase master spec has been built and
verified except the two items below. In brief: OTP access, agent portal,
DB-backed Website Settings (branding/pricing/social/Help Shadi naming),
banners, testimonials, shop, Coins wallet, CSRF, security headers, rate
limiting, photo privacy + watermarking, consent declaration,
recommendations, pagination, DB indexes, FAQ chatbot, SEO, WAL/
busy_timeout + explicit retry-on-lock, `/healthz`, welcome overlay, Help
Shadi (with admin-editable display name), a live payment waiting-lounge
stepper with polling, social media links, a real admin notification
feed (distinct from the visitor-facing announcement banner), branded
429/500/503/etc. error pages, lazy-loaded below-the-fold images, and a
one-click in-app backup system plus a standalone `backup.py` for cron —
full detail and verification notes for every one of these are in
`CHANGES_PART1.md`–`CHANGES_PART4.md`. If you think something here is
missing, grep for it first — the audit trail shows exactly what was
checked and how.

## What's genuinely still outstanding

Only these two — both are blocked by environment limitations, not
unfinished work, across every session so far:

### 1. Real load/concurrency testing (spec Phase 38)

Nobody has run this app under real concurrent write load yet. WAL mode,
`busy_timeout=5000`, and an explicit commit-retry wrapper are all in
place and *should* handle it, but that's a prediction, not a measured
fact. If you have access to an environment that can actually hit a real
running instance with concurrent requests (`locust`, `ab`, or similar —
not `app.test_client()`, which doesn't simulate real concurrency or
network conditions), do that and report real numbers: requests/sec,
median and 95th-percentile latency, error rate, any lock errors, under
something like 50–100 concurrent browsing/search users plus smaller
concurrent registration/payment activity. If you're in the same kind of
sandboxed environment as every prior session, say so plainly rather than
guessing or inventing numbers.

### 2. Real-browser visual QA (spec Phase 39)

Every UI change across all four parts has been verified by reading
CSS/markup and confirming it reuses existing classes/patterns, plus
functional `app.test_client()` tests that confirm the right elements
render — nobody has actually looked at any of it in a real browser, on
mobile or desktop. If you have real browser access, do a visual pass
across 360/375/390/414/768/1024px and desktop, specifically on: the
welcome overlay, Help Shadi pages, the new payment waiting-lounge
stepper, the footer social icons, admin notifications feed, and the
admin backup page. If you don't have browser access either, don't fake
a visual QA pass — say so and leave it open.

### Minor, low-priority, optional if you're already in the file

`static/css/style.css` still has roughly 20 low-opacity ambient
box-shadow values (card hover shadows, banner shadows) using a
hardcoded green tint like `rgba(11,90,68,.06)`, left over from before
Part 3's header/footer theme-color fix. These are barely visible
(shadows, not solid fills) and only noticeable on a close look at a
non-green theme preset. Not urgent on its own — only worth a pass if
you're already touching that file for something else.

## If the person asks for something new instead

Treat it as a fresh task: inspect the current code first (state may
have changed since this file was written), then follow the same
non-negotiable rules at the top.

## Reporting requirement

Same as every prior part: tell the person exactly which files you
touched, what you verified vs. couldn't and why, and update this file
(add `CHANGES_PART5.md`) so nobody repeats work either. Give them a
complete ready-to-deploy zip, not a diff.
