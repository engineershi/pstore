# trypstore.com — Operator Context

> **EXPERT MODE is active by default in every session** (see
> `~/.config/opencode/AGENTS.md`). Say "switch to expert mode" or run
> `/expert-mode` to re-assert it; say "standard mode" to drop it.

Fill any field marked _TBD_ from the code, and keep this file current — this is
the input the operator mode reasons from.

## PROJECT
Keyless Amazon affiliate niche-finder + content engine (codename `pstore`),
deployed at trypstore.com. Public site is fully crawlable SEO surface; admin
section is the owner's operating console. Zero API keys required to earn —
`?tag=` affiliate links work from boot.

## STACK
- Stdlib Python (`http.server`, `sqlite3`, `urllib`) — no framework, no deps.
- One file per concern in `app/`: `amazon.py`, `niche.py`, `seo.py`,
  `editorial.py`, `market_engine.py`, `server.py`.
- SQLite at `PSTORE_DB` (default `app/pstore.db`); **must** be a mounted volume
  or every deploy wipes subscribers/settings/clicks/earnings back to seed.
  `app/pstore.db` is a **tracked 43-niche seed** — `git checkout -- app/pstore.db`
  if a test run grows it.
- Secrets: `PSTORE_ADMIN_EMAIL`, `PSTORE_ADMIN_PASSWORD`, `PSTORE_HASH_SECRET`,
  `PSTORE_OAUTH_SECRET`, `PSTORE_TAG`, `PSTORE_MARKET`, `PSTORE_URL`.
- Optional: `OAUTH_GOOGLE_*` / `OAUTH_FACEBOOK_*`; ScraperAPI/Outscraper/SerpAPI
  proxies in Settings; `PAAPI_ACCESS_KEY` / `PAAPI_SECRET_KEY` /
  `PAAPI_PARTNER_TAG` / `PAAPI_HOST` / `PAAPI_MIN_INTERVAL` (see CURRENT
  BLOCKER). PA-API is throttled to ~1 req/s and the client paces itself to
  match; 429/500/503/504 are retried, 401/403 are not (they mean bad keys).
- Deploy: Fly.io (`fly.toml`, `Dockerfile`) + Render (`render.yaml`).
- Tests: `cd app && python3 -m unittest discover -s tests` (~1330, ~7 min, all
  offline). Admin login for live checks is in aimem (`trypstore-com`).

## AUDIENCE / ICP
Dual audience, keep separate — they have opposite economics:
1. **Buyers** (searchers on `/n/<niche>`): click through to Amazon on a `?tag=`
   link. Never pay them. Revenue = commission only.
2. **Owners/operators** (solo creators, ex-Amazon-sellers, side-hustlers
   hunting a profitable niche): low cash, no API keys, want revenue fast.
   **This is the only audience that can pay us directly, and nothing is
   currently for sale to them.**

## OFFER + PRICE
- Today: free site, 100% of revenue is Amazon commission via `PSTORE_TAG`.
  Every page must end in a buyer click.
- A paid offer is **built but unlaunched**: `payments.py`, `/pro`, the PDF
  delivery (`pdfgen.py`) and the Stripe webhook all exist. `payments.sellable()`
  deliberately refuses to publish the offer unless BOTH `paid_checkout_url` and
  `stripe_webhook_secret` are set — a checkout link with no working webhook takes
  money and never grants access. So `/pro` 404s and stays out of the sitemap
  until the owner configures both.
  **Owner action:** create the Stripe Payment Link, set `paid_checkout_url` and
  the webhook signing secret (`/webhook/stripe`), then re-verify. Highest-leverage
  unbuilt revenue line — this is the only audience that can pay us directly.
- Commission is category-driven, not flat: see `earnings.CATEGORY_AOV` /
  `SAMPLE_CATEGORIES`. Per-click value ranges **$0.03 (grocery) to $0.51
  (jewelry)** — an 17x spread on identical traffic. Prefer jewellery, tools,
  furniture, appliances, garden. Never value a page at the 4% / $40 default.
- Commission is category-driven, not flat: see `earnings.CATEGORY_AOV` /
  `SAMPLE_CATEGORIES`. Per-click value ranges **$0.03 (grocery) to $0.51
  (jewelry)** — an 17x spread on identical traffic. Prefer jewellery, tools,
  furniture, appliances, garden. Never value a page at the 4% / $40 default.

## TOP 3 METRICS
1. **Indexed pages that earn an impression** (currently: Google/Bing/Yandex/DDG
   all report 0 views — this is the binding constraint, not conversion).
2. Organic session → click-through to Amazon (revenue per session).
3. Email list growth + repeat purchase (the only compounding asset).

## MOAT
Candidate: the free keyless niche-finder nobody else ships, GSC/Bing/Yandex +
IndexNow plumbing already built and connected, and a captured email list.
**The `/n/<parent>/<term>` long-tail inventory is NOT a moat and must not be
described as one** — 1,963 of those pages were relabels of their own parent
(100% shared ASINs, 89.6% shared body text). That surface is now auto-held by
`server._thin_topic_paths`. Extend the *real* inventory instead: price-band and
vs head-to-head pages, which answer a distinct query.

## CURRENT BLOCKER
1. **Zero discovery.** 1,322 URLs are crawlable, 200 OK, self-canonical,
   `index,follow` — and Google, Bing, Yandex, DuckDuckGo and Yahoo have all sent
   **0 views**. 1,102 lifetime pageviews, 28 clicks, $1.35 modelled, **$0 real**.
   13,212 social posts and 2,154 Pinterest pins have produced **0 clicks**.
   Crawl and schema are now clean (verified live: 40/40 sampled `/n/` URLs
   indexable, sitemap/render agree). What remains is authority + a young domain
   competing against Amazon itself for head commercial terms.
   Fixed on our side: `/niches` indexes the whole inventory (live, page 1 in the
   sitemap, `Server-Timing` reporting), and `related_niches()` is topical instead
   of a fixed six (live: 10 pages → 55 distinct targets, reuse only where the
   match is real). **Neither has had time to earn an impression — check Search
   Console before drawing any conclusion from the traffic number.**
2. **PA-API credentials unset in prod.** The code path is wired and verified; the
   input is missing. Until then: all product data comes from TOS-violating
   scraping (account-ban risk to the whole stream) and **0 ranking pages carry a
   product image**, which is why merchant-listing rich results never fire.
   **Owner action, exact order:** save the three keys in `/admin/keys` → *Test
   PA-API* → *Backfill images from PA-API* (`POST /api/paapi/backfill`). The
   backfill is mandatory, not optional: niches saved before the keys existed were
   never enriched, and `seo.py` only renders an image when `source == "paapi"`.
   Do not re-mine instead — that re-scrapes every niche for nothing.
3. No vertical chosen; consolidation holds have still never been fired.
4. **Cloudflare serves every URL as `cf-cache-status: DYNAMIC`**, including
   `/style.css`, despite the origin sending `s-maxage`. Origin TTFB is 87–117 ms
   while live TTFB is 1.0–7.3 s, so the crawl delay is edge-side, not our
   rendering. Needs a dashboard Cache Rule (owner action; no API creds here).

## The `source == "paapi"` rule (do not "simplify" it away)
`amazon.licensed_rating()` returns ratings **only** for `source == "paapi"`, and
`seo.py` renders a product image only on the same condition. That is a licensing
chokepoint, not a filter: a scraped star rating is Program Content we are not
licensed to republish. So:
- `_enrich_products()` merges PA-API fields into an existing scraped row and, via
  `_mark_paapi_sourced()`, promotes it to `source == "paapi"` — **but only when
  the row holds no scraped rating.** A legitimate PA-API image does not license
  a scraped star rating sitting in the same dict.
- A price-only enrichment must never promote the row (nothing reader-facing was
  added). The guard reads `((took & ratings) | ({"image"} if "image" in took))` —
  the parentheses are load-bearing; `&` binds tighter than `|`.
- Any change that makes a non-PA-API field visible to readers bypasses the
  Associates programme. Treat "it renders now" as a red flag, not progress.

## Rules for this repo (house style)
- Run `python3 -m unittest discover -s tests` before claiming done; keep green.
- Admin pages stay `noindex` and behind auth; never ship a secret or a fallback
  default password.
- **Never list a URL in the sitemap that the page renders as noindex.** The
  sitemap filter and the page's robots meta must agree; `_force_noindex` is the
  chokepoint and `_thin_topic_paths()` feeds it.
- When a page's content cannot be distinguished from another page's, it does not
  get its own indexable URL. Relabelling is not a page.
