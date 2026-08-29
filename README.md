# Saif Matrimonial Services — v3

A rebuilt, business-ready version of the matrimonial platform. Browsing is
free; unlocking a specific profile costs a small fixed fee (₹11 by
default, editable), verified manually by the admin from a payment
screenshot. No accounts/passwords for visitors — access is by mobile
number + OTP.

## What's new in v3 (vs the v2 zip you uploaded)

v2 already had solid bones (private photo storage, CSRF, security
headers, rate-limited admin login, edit profiles, custom error pages) —
that groundwork was kept. v3 adds the things a real launch needs:

| Area | v2 | v3 |
|---|---|---|
| **"My Requests" access** | Trusted whatever phone number was typed into a form — anyone could view anyone's requests by typing their number + a leaked/guessed code | Real **OTP verification**. Enter your number → receive a 6-digit code → verified. That session is the only thing that unlocks "My Requests". |
| **Branding** | Hardcoded in `app.py` env vars and in HTML text (`base.html` literally said "Saif Matrimonial") | **DB-backed Website Settings page** (`/admin/settings`): name, tagline, phone, WhatsApp, email, location, price, UPI ID, 3 brand colors, hero text, logo, favicon — no HTML editing needed. |
| **Banners** | None | Hero / mid-page / footer banner slots, admin-manageable, with optional link + date range. |
| **Search/filters** | None — homepage just listed everything | Real server-side filters (gender, age range, city, marital status, education, profession, community) + pagination (12/page) + results count + clear filters. |
| **Contact number** | Always shown once unlocked | Per-profile "show contact directly?" toggle — if off, the unlocker is routed to WhatsApp instead. |
| **Photo watermarking** | None (photo access was already private/authorized, but the served file was a plain copy) | Full-resolution photos served to an unlocked viewer are watermarked per-viewer (brand + profile code + phone hash), so a leaked copy is traceable. |
| **Verification badges** | A single "Verified" concept, unused | Three honest, separate badges: **Admin Reviewed**, **Phone Verified**, **Photo Reviewed** — admin ticks only what's actually true. |
| **SEO** | None | `robots.txt`, `sitemap.xml`, `noindex` headers on private pages, canonical URLs. |
| **FAQ / Terms** | Only Privacy existed | Added dedicated FAQ and Terms of Use pages. |
| **Admin activity log** | None | Every login, profile add/edit/delete, request decision, settings/banner change is logged with admin username, IP, timestamp — visible on the dashboard. |
| **Database** | Basic schema | Extra profile fields (state, sect, mother tongue, height, income, work location, family details) + indexes on the columns that actually get filtered/searched, matching what you listed. Non-destructive migration — safe to deploy over an existing v2 database. |

## How "My Requests" access works now (this is also "registration")

There's no separate sign-up screen, on purpose — this business doesn't
need one. The phone number someone uses when submitting an unlock
request **is** their account. To check status from any device:

1. Go to "My Requests" → enter mobile number.
2. We generate a 6-digit OTP, store only its hash (5-minute expiry), and
   send it via `send_otp_sms()` in `app.py`.
3. **Out of the box, no SMS account is connected** (I don't have API
   keys for MSG91/Fast2SMS/etc., and this environment couldn't reach the
   internet to test one even if I did). So by default `OTP_PROVIDER=console`:
   the OTP is written to the server log **and shown directly in the
   on-site flash message** so you can test the whole flow immediately
   without paying for SMS.
4. To send *real* SMS in production: sign up with an Indian SMS/OTP
   provider (Fast2SMS and MSG91 are both wired up already — see
   `.env.example`), set `OTP_PROVIDER` + the provider's API key as
   environment variables, and redeploy. No code changes needed —
   `send_otp_sms()` is one isolated function.
5. Once the code is entered correctly, the session is marked verified
   for that phone (45-minute session, matching the cookie lifetime) and
   "My Requests" / unlocked full-profile pages become visible.

This is real, working OTP — the only thing missing is a paid SMS
account, which I can't create on your behalf.

## Photo protection (kept from v2, now also watermarked)

1. Admin uploads a photo → validated as a real image, EXIF/GPS stripped,
   re-encoded, saved under a **private, non-public folder** with a random
   filename.
2. A separate blurred, low-res preview is generated for public browsing
   (`static/previews/`).
3. The full photo is only ever served through `/profile/<code>/photo`,
   which re-checks on **every single request** whether the current
   session has an *unlocked* request for that *exact* profile.
4. On top of that, the copy served is generated fresh per-viewer with a
   subtle tiled watermark (brand name + profile code + a hash of the
   viewer's phone), cached to disk. `Cache-Control: no-store`.

**Honest limit, stated on purpose:** no website can stop someone from
screenshotting what's already on their own unlocked screen. This system
stops casual downloading, direct-linking, and scraping — and makes any
leaked copy traceable back to who unlocked it.

## Running locally

```bash
pip install -r requirements.txt
python app.py
```

Opens on `http://localhost:5000`. Admin: `admin` / `changeme123` by
default — **change this** via `ADMIN_USERNAME` / `ADMIN_PASSWORD`
environment variables before going live.

## Before deploying for real

1. Set env vars from `.env.example` — at minimum `SECRET_KEY`,
   `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SESSION_COOKIE_SECURE=true`.
2. Log in to `/admin/settings` and set your real business name, phone,
   WhatsApp number, UPI ID, price, colors, logo.
3. Add your real UPI QR code image at `static/upi_qr.png`.
4. Set `OTP_PROVIDER` + its API key so OTPs actually reach users by SMS
   (see above) — without this, OTPs only show up in the server log,
   which is fine for your own testing but not for real users.
5. **Add persistent storage.** On Railway/Render, attach a volume and set
   `DATA_DIR` to its mount path (e.g. `/data`) — otherwise the database,
   uploaded photos, branding/banner images, and payment screenshots are
   wiped on every redeploy.
6. Review `/privacy` and `/terms` wording and adjust to your actual
   practices.

## Deployment (Railway, Render, or any host that runs gunicorn)

```
gunicorn app:app
```

(already in `Procfile`). `debug=False` is hardcoded for the dev
entrypoint — never runs with debug on.

### GitHub + Railway

This chat can't push to GitHub or deploy to Railway directly (no network
access from this environment, and no account credentials of yours). Two
ways to get this code live:

- **Manual (works right now):** download the zip below, create a new
  GitHub repo, push this code to it, then in Railway choose "Deploy from
  GitHub repo" and point it at that repo. Add the env vars from
  `.env.example` in Railway's dashboard, attach a volume, set `DATA_DIR`.
- **Connector:** if you connect a GitHub (or Railway) connector in this
  chat, I can create/push the repo and open the deployment for you
  directly next time, instead of you copying files by hand.

## Project structure

```
matrimonial_v2/
├── app.py                      # all routes, security, image + OTP logic
├── requirements.txt
├── Procfile
├── .env.example
├── .gitignore
├── static/
│   ├── css/style.css
│   ├── js/main.js
│   ├── previews/                # PUBLIC — blurred previews only
│   ├── branding/                # PUBLIC — logo/favicon uploaded via admin
│   ├── banners/                 # PUBLIC — banner images uploaded via admin
│   └── upi_qr.png               # ADD YOUR REAL QR CODE HERE
├── storage/private/              # NEVER public — originals, proofs, watermark cache
│   ├── profile_originals/
│   ├── payment_proofs/
│   └── watermark_cache/
└── templates/                    # all pages, including admin + error pages
```

## Known trade-offs (documented on purpose, not hidden)

- **Rate limiting and OTP storage state are in-memory for the limiter,
  DB-backed for OTP codes** — fine for a single small worker; the
  limiter won't share state across multiple gunicorn workers/dynos at
  larger scale. Move to Redis-backed limiting if you outgrow this.
- **SQLite** — simple and sufficient at this scale; the schema uses
  plain parameterized SQL (no ORM), so migrating to PostgreSQL later is
  a contained change, not a rewrite — swap the `sqlite3.connect` calls
  for a Postgres driver and adjust the few SQLite-specific bits
  (`INSERT OR IGNORE`, `ON CONFLICT`).
- **OTP delivery needs a paid SMS provider for real users** — see above.
- **Payment is still manual UPI, on purpose** — matches the business
  model you described; no automatic payment verification is claimed
  anywhere in the UI.
