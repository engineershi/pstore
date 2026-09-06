# pstore

## App root
`/root/projects/mazon/app` — Amazon affiliate niche-finder. Backend is stdlib
Python (`http.server`, `sqlite3`, `urllib`). No framework, no install.

## Git note (environment)
The sandbox filesystem has a broken `link()` on `/root/projects` (returns ENOENT),
so a normal in-tree `.git` cannot write loose objects. The working repo keeps its
gitdir at `/root/.gitdirs/mazon` and `/root/projects/mazon/.git` is a gitfile:
```
gitdir: /root/.gitdirs/mazon
```
Do not `rm -rf .git` or `git init` in-tree here. To add the repo remote / push,
operate normally from `/root/projects/mazon` (git auto-reads the gitfile).

## Run
- Server: `python3 server.py` (serves `static/` + `/api/*` on port 8765).
- Env: `PSTORE_TAG=<your-tag>-NN`, `PSTORE_MARKET=com` (default),
  `PSTORE_ADMIN_EMAIL=<email>` + `PSTORE_ADMIN_PASSWORD=<pw>` (gates /admin,
  /dashboard, /tool, /keys, /api/*; falls back to a default with a startup
  warning when either is unset).
- Optional social login: set `OAUTH_GOOGLE_CLIENT_ID`/`OAUTH_GOOGLE_CLIENT_SECRET`
  and/or `OAUTH_FACEBOOK_APP_ID`/`OAUTH_FACEBOOK_APP_SECRET` (with `PSTORE_URL`
  set to the live origin). Redirect URIs are `<PSTORE_URL>/admin/oauth/google/callback`
  and `<PSTORE_URL>/admin/oauth/fb/callback`; only the admin email is granted.
  Unset providers get no button on the login page.
- Multi-user RBAC: self-registration (`/admin/register`) requires an emailed,
  HMAC-signed confirmation link before login; the env owner alone manages users
  and the role matrix (`/admin/users` + `/api/users`). Every admin page/API is
  owned by exactly one function (`FUNCTION_PATHS`); a user's access is the union
  of their roles' functions, enforced server-side (403) on pages AND their APIs.
  Owner handles are session-uid `None`; team sessions carry a user id. Passwords
  are PBKDF2-HMAC-SHA256 (`security.hash_password`/`verify_password`).

## Security model
- `security.py` = per-client rate limiters (login 5/15min, /api 240/min, global
  720/min), concurrency semaphore (503), 413 body / 414 URI caps, HMAC-signed
  state tokens for OAuth + CSRF. `oauth.py` = stdlib OAuth2 (urllib) for Google
  and Facebook.
- Every response carries CSP, X-Frame-Options, nosniff, Referrer-Policy,
  Permissions-Policy (and HSTS behind TLS). Cookies are HttpOnly + SameSite=Lax
  (+Secure over HTTPS). State-changing POSTs reject cross-origin Origin headers.
- All SQL is parameterized (`?` placeholders) — keep it that way; never build
  queries with string interpolation.

## Tests
```
cd /root/projects/mazon/app
python3 -m unittest discover -s tests -v
```
All tests are offline: they stub `amazon._urlopen` and keep `CACHE_TTL=0`,
`MIN_INTERVAL=0`. After changing backend code run the full suite; keep it green.

## Release checklist (every user-visible change)
- Run the full suite from `/root/projects/mazon/app` before pushing; keep it green.
- If URLs/pages/slugs change: regenerate and verify `sitemap.xml` covers the new
  paths (any change to niches/lead pages re-renders it), then ping IndexNow
  (`/api/indexnow` POST or the admin SEM page) so search engines re-crawl.
- Update `manual.py` (admin manual + PDF) and this repo's own guide/info pages so
  the docs match the shipped UI; rename tool labels everywhere they appear.
- Commit + push `origin master`, deploy via the Render API, poll until `live`,
  then live-verify the touched page/endpoint (admin login + HTTP checks).
- Email features: verify a dry-run and a real send, plus the tracked `/e/` link,
  open pixel and unsubscribe link, all signed with the durable
  `PSTORE_HASH_SECRET`/`PSTORE_OAUTH_SECRET` env secrets (never per-boot randoms).
- Save a dated note via `aimem note --project mazon` summarizing what shipped
  and how to resume.

## Conventions
- Stdlib only; no third-party imports.
- `amazon.py` = keyless Amazon product data (search, autosuggest, scraper
  providers, affiliate URL builder). `niche.py` = mining. `seo.py` = crawlable
  SSR pages. `market_engine.py` = buyer-push link/text tools. `server.py` = HTTP.
- Every network call bottoms out in module-level `amazon._urlopen` so tests can
  inject fake responses; providers never raise on the happy path.
- Affiliate links are always direct and tagged via `amazon.affiliate_url(asin)`
  (or `market_engine.redirect_url(asin)`); never hardcode `?tag=` and never
  generate cloaked `/go/` links in new output (kept only as a legacy resolver).
- New tests under `tests/` as plain `unittest` classes.