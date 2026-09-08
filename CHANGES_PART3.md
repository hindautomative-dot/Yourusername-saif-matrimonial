# Saif Matrimonial — Part 3 Changes

Small, targeted fixes requested directly by the business owner after
comparing the site against the original 40-phase master spec. No
rebuild, no destructive changes — same additive rules as Parts 1–2.

## 1. Authentic Bismillah spelling

`templates/base.html` — the welcome-overlay text
`Bismillah-ir-Rahman-ir-Rahim` was replaced with the more standard
transliteration `Bismillah hir Rahman nir Raheem` (the linking "hir"/"nir"
reflects how the phrase is actually pronounced/written in Arabic, and
"Raheem" is the more common English transliteration than "Rahim").

**Note on "Shubh Shuruat"**: the master spec's Phase 9 suggested this
phrase for the ₹11 registration CTA. It was never actually added to the
codebase in Part 1 or 2 (confirmed by grep before this session started),
and per the owner's explicit instruction it must **not** be added — the
word carries a Hindu-festival connotation that doesn't fit an Islamic
matrimonial service. Left out of `CONTINUE_PART4_PROMPT.md` for the same
reason — a future session shouldn't add it either.

## 2. Theme header/footer color-matching bug (fixed)

**Root cause**: `.site-header`, `.site-header.scrolled`, `.nav-mobile`,
and `.site-footer` in `static/css/style.css` used **hardcoded green
`rgba(...)` literals** (e.g. `rgba(7,62,47,.97)`) instead of the theme's
`var(--emerald)` / `var(--emerald-dark)` CSS variables. So when the
admin switched to any of the other 4 theme presets in Appearance
Settings (Midnight & Gold, Burgundy & Champagne, Royal Plum, Olive &
Sand), every other section of the site correctly changed color — except
the header, mobile nav menu, and part of the footer gradient, which
stayed green regardless. That's exactly the "ganda/bekaar" mismatch you
flagged.

**Fix**: replaced every hardcoded green literal in those 4 CSS blocks
with `color-mix(in srgb, var(--emerald-dark) X%, transparent)` (same
`color-mix()` technique already used elsewhere in this stylesheet, so no
new browser-support assumption was introduced). Also fixed two more
visible spots using the same hardcoded green: the banner-tag pill
background and the profile-photo lock overlay. The footer's gradient
end-stop (`#052b20`, a hardcoded dark green) was replaced with
`color-mix(in srgb, var(--emerald-dark) 80%, black)`, so it darkens
*from whatever the current theme's color is* instead of always fading
to green.

**Also fixed**: `--gold-soft` (used for footer link text and a few
accent touches) was a fixed hex value (`#e8d9b8`) that never changed
with the theme at all. It's now computed in `base.html`'s inline
`:root` block as `color-mix(in srgb, {{ accent_color }} 55%, white)`,
so footer text tone now tracks whatever gold/accent color the selected
theme actually uses.

**What I did NOT touch**: there are ~20 low-opacity ambient box-shadows
elsewhere in the stylesheet (card hover shadows, banner shadows, etc.)
that also use a hardcoded green tint, e.g. `rgba(11,90,68,.06)`. These
are barely visible (shadows, not solid fills) and changing all of them
is a much bigger, higher-risk edit for very little visible difference —
left as a nice-to-have in `CONTINUE_PART4_PROMPT.md` rather than risking
a rushed CSS-wide find/replace.

**Verified**: loaded the homepage in a test client, confirmed
`--emerald` reflects the saved `primary_color` setting and `--gold-soft`
is now a `color-mix(...)` expression instead of a fixed hex — both
render correctly, and the previously-existing pages still return 200
with the new CSS in place.

## 3. Help Shadi display name — now admin-editable

Added a new setting, `help_shadi_display_name` (default: `"Help
Shadi"`), editable from **Admin → Website Settings** under a new "Help
Shadi" section. Changing it updates the name everywhere it's shown to
visitors and to the admin — nav (desktop + mobile), footer, the
`/help-shadi` and `/help-shadi/register` page headings and button
labels, the admin request list page, the admin dashboard's stat cards
and quick-action button, and the admin alert sent on a new submission.

**The URL never changes** — `/help-shadi` and `/admin/help-shadi` stay
exactly as they are no matter what the admin renames it to, so
bookmarks, the nav links, and anything already pointing at those routes
keep working. Only the *label* is editable, not the route.

**Verified** with a real `app.test_client()` run: renamed it to "Nikah
Sahara" through the settings form, then confirmed the new name appears
on the public page, the register page, the homepage nav, the admin list
page, and the admin dashboard — and that the old "Help Shadi" text is
gone from the public page once renamed. A full regression pass on 7
existing routes afterward still returned 200.

## Files touched

| File | Change |
|---|---|
| `templates/base.html` | Bismillah spelling; nav "Help Shadi" → `{{ help_shadi_name }}`; `--gold-soft` now theme-derived |
| `static/css/style.css` | Header/nav-mobile/footer/banner-tag/photo-overlay now use theme CSS variables instead of hardcoded green |
| `app.py` | New `help_shadi_display_name` setting (default + allow-list + context processor); `notify_admin()` call for Help Shadi now uses the custom name |
| `templates/admin_settings.html` | New "Help Shadi" section with the display-name field |
| `templates/help_shadi.html`, `help_shadi_register.html`, `help_shadi_submitted.html`, `admin_help_shadi.html`, `admin_dashboard.html` | Hardcoded "Help Shadi" text replaced with `{{ help_shadi_name }}` |

No table/column changes, no route renames, nothing removed.
