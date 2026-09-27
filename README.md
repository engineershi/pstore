# pstore — Amazon Affiliate Niche Finder

Keyless Amazon affiliate marketing: find niches, display products, push buyers.
No Amazon API key required to start — you earn from day one with `?tag=` links.

## Stack
- Stdlib Python (`http.server`, `sqlite3`, `urllib`) — no framework, no install.
- One file per concern: `amazon.py` (keyless product data), `niche.py`
  (niche mining), `seo.py` (crawlable pages), `editorial.py` (ranked verdict
  content + JSON-LD), `market_engine.py` (buyer-push tools), `server.py` (HTTP).

## Run
```
cd app
python3 server.py            # serves http://localhost:8765
```
Set env: `PSTORE_TAG=youraffiliate-20`, `PSTORE_MARKET=com` (or co.uk/de/ca/in/...).
Set `PSTORE_ADMIN_EMAIL` + `PSTORE_ADMIN_PASSWORD` to lock the owner section
(dashboard, tools, keys, APIs). Without them the server generates a random
single-boot admin password, prints it, and warns — it never falls back to a
well-known default. Set `PSTORE_HASH_SECRET` and `PSTORE_OAUTH_SECRET` to long
random strings too: without them a new random value is generated at every boot,
which invalidates signed unsubscribe links, PDF-gate tokens and in-flight OAuth
logins on each restart. Optional Google/Facebook login: set
`OAUTH_GOOGLE_CLIENT_ID`/`OAUTH_GOOGLE_CLIENT_SECRET` or
`OAUTH_FACEBOOK_APP_ID`/`OAUTH_FACEBOOK_APP_SECRET` (with `PSTORE_URL`) to add
"Continue with Google/Facebook" buttons on the login page; only the admin email
wins a session.

### Persistence
The database is a single SQLite file at `PSTORE_DB` (default
`app/pstore.db`). **If `PSTORE_DB` is not set it lands inside the container
image, and every deploy replaces it** — subscribers, settings, clicks, earnings
and social posts revert to the baked 43-niche seed. Mount a persistent disk and
point `PSTORE_DB` at it (`/data/pstore.db`); the shipped catalogue is copied in
automatically on first boot, so an empty volume still comes up with the niches.
The server prints a `WARNING` at boot and lists it in `/admin/system` when this
is misconfigured.

## Two parts
- **Public site** — fully crawlable, no login: `/` landing, `/n/<niche>`
  reviews, `/n/<parent>/<term>` long-tail topic pages, `/lp/<niche>` sales
  pages, about/legal, `sitemap.xml`, `robots.txt`.
- **Admin section** — password-gated owner tools, reachable from `/admin`
  (login = `PSTORE_ADMIN_EMAIL` / `PSTORE_ADMIN_PASSWORD`). `/admin` shows a button to every
  page on the site (admin tools + every public page + API endpoints); it also
  houses the niche finder dashboard (`/dashboard`), marketing suite (`/tool`)
  and keys page (`/keys`). Admin pages are `noindex` and never crawlable.

## What it does
- **Product search / display** — keyless: direct Amazon page parse, or plug in
  a ScraperAPI / Outscraper / SerpAPI key in Settings for a proxy.
- **Niche mining** — Amazon autosuggest demand proxy + saturation scoring.
- **SEO pages** — saved niches get crawlable `/n/<slug>` pages with meta,
  OpenGraph, JSON-LD (Product/ItemList/FAQ/Breadcrumb/Organization), canonical;
  `sitemap.xml` + `robots.txt` auto-generated.
- **Long-tail reach** — `/admin/opportunities` "Build long-tail pages" (or
  `POST /api/topics/generate`) creates nested `/n/<parent>/<term>` pages from
  live Amazon autosuggest terms (ranked ItemList schema, breadcrumbs, links
  back to the parent hub), listed in the sitemap and pinged to IndexNow.
- **Conversion on `/n/`** — public verdict pages carry reciprocal internal
  links (`related`), a live-price urgency line, and a scroll/exit-intent sticky
  "see on Amazon" CTA pointing at the #1 pick.
- **Buyer-push tools** (`/tool`) — copy-paste affiliate text links, Markdown,
  email draft, social post; `/go/<ASIN>` short redirects to tagged Amazon.
- **Native social publish** — `app/publish.py` posts saved kits straight to
  X/Pinterest/Facebook/LinkedIn once you paste the platform app keys into
  Settings (`/admin/apikeys`).

## Security
- Rate limiting (per client login/API/global), a concurrency cap, 413/414 size
  limits, cross-origin POST rejection, and HMAC-signed OAuth state tokens.
- CSP, clickjacking, MIME-sniffing, referrer and HSTS headers on every response;
  HttpOnly/SameSite/Secure cookies. All SQL is parameterized.
- Hardened by design, but no software is invulnerable — keep Render free-tier
  instances current (stale instances intermittently time out).

## Tests
```
python3 -m unittest discover -s tests -v
```
All offline (stub `amazon._urlopen`); no keys or network needed.