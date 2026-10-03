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
  proxies in Settings; `PAAPI_*` (see CURRENT BLOCKER).
- Deploy: Fly.io (`fly.toml`, `Dockerfile`) + Render (`render.yaml`).
- Tests: `cd app && python3 -m unittest discover -s tests` (~1197, ~10 min, all
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
- **No owned revenue exists.** No Stripe/PayPal/Gumroad anywhere in the repo
  (the only "premium" in the code is a CSS theme preset). The delivery machinery
  for a paid product already exists unused: `pdfgen.py` (PDF), `cms.py` (page
  editor), email gate. Highest-leverage unbuilt revenue line.
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
1. **Zero discovery.** 2,469 `/n/` URLs are crawlable, 200 OK, self-canonical,
   `index,follow` — and Google, Bing, Yandex, DuckDuckGo and Yahoo have all sent
   **0 views**. 1,102 lifetime pageviews, 28 clicks, $1.35 modelled, **$0 real**.
   13,212 social posts and 2,154 Pinterest pins have produced **0 clicks**. This
   is not a crawl or schema problem (verified); it is authority + a young domain
   competing against Amazon itself for head commercial terms.
2. **PA-API credentials unset in prod.** The code path is wired and verified; the
   input is missing. Until then: all product data comes from TOS-violating
   scraping (account-ban risk to the whole stream) and **0 ranking pages carry a
   product image**, which is why merchant-listing rich results never fire.
3. No vertical chosen; consolidation holds have still never been fired.

## Rules for this repo (house style)
- Run `python3 -m unittest discover -s tests` before claiming done; keep green.
- Admin pages stay `noindex` and behind auth; never ship a secret or a fallback
  default password.
- **Never list a URL in the sitemap that the page renders as noindex.** The
  sitemap filter and the page's robots meta must agree; `_force_noindex` is the
  chokepoint and `_thin_topic_paths()` feeds it.
- When a page's content cannot be distinguished from another page's, it does not
  get its own indexable URL. Relabelling is not a page.
