# n8n: Pstore automations

Self-hosted, unlimited, zero per-run cost. Three imported workflows, one
credential convention.

> **2026 update — n8n is now optional.** pstore ships its own free router at
> `POST /api/social/webhook` (`app/server.py` → `_social_webhook`). Point the
> `SOCIAL_WEBHOOK` env var at `https://<your-pstore>/api/social/webhook` (or any
> Make/Zapier URL) and pstore fans kits out **natively** via the keys pasted on
> `/admin/apikeys` — Pinterest, X, Facebook and LinkedIn for free, with one
> retry, recorded honestly in `social_posts`, no Node.js box and no budget. The
> n8n flows below stay as the reference only for platforms without a native
> backend yet (Instagram, Threads) and fancier out-of-band scheduling.

## Files
- `pstore-rss-to-pinterest.json` — the main workflow: watches
  `https://pstore-gxbv.onrender.com/rss.xml`, pins each new niche to Pinterest.
  Defaults to every 6 hours, max 3 pins per run (Pinterest's own guides suggest
  10–25/day max — drip, not burst). Needs `n8n-nodes-pin-interest` + your
  approved Pinterest dev app.
- `pstore-pin-healthcheck.json` — nightly (09:30 UTC) report of which pin
  niches actually earned clicks. Logs into pstore, reads
  `/api/pin-health?days=90` (per-niche pinterest-source clicks), alerts on
  Telegram when niches came up empty. Needs only pstore admin creds + a
  Telegram bot — no Pinterest app required.
- `pstore-fanout-social.json` — fan-out hub: one webhook, many socials. POST a
  pin-shaped payload and it mirrors to every channel whose flag is `true` in
  the "Normalize payload" node's `TOGGLES` constant. Telegram photo works with
  zero approval; Facebook page photo + Mastodon status need their own tokens.
- `pinterest-list-boards.json` — run once to discover the numeric Board ID.
- `pstore-social-router.json` — the everything router: point pstore's
  `SOCIAL_WEBHOOK` at this webhook and one verified package posts to **all six**
  platforms whose toggle is ON (X, Facebook, LinkedIn, Instagram, Pinterest,
  Threads). Uses plain HTTP Request nodes; per-platform setup below.

## Why n8n (vs Make/Pipedream)
- Make / Pipedream: Pinterest "Create a Pin" is Premium/own-token-walled.
- n8n: self-hosted Community Edition is **free and unlimited**; the
  Pin-Interest node handles OAuth token refresh + board targeting so you never
  bake in an expiring token.
- Make's "connect a social without your own account" is Make **Center** — Make's
  *shared* test accounts, and only for a few consumer apps (Viber, Messenger
  test page, some WhatsApp/Telegram flows). It never works for Pinterest, X,
  Instagram or Threads — those platforms require **your own account + app
  approval on every tool**. n8n's equivalent is the fan-out hub above, with
  your credentials stored on your own box.

## The one hard requirement (approx. 7-day wait)
Every Pinterest-API path — including this — needs **your Pinterest developer
app approved** at `developers.pinterest.com`:
1. Convert to a Business account (already done).
2. Create an app; note Client ID + Client Secret.
3. Add scopes `pins:write`, `boards:read`, `user_accounts:read`.
4. Set the OAuth redirect URI to `https://<your-n8n-host>/rest/oauth2-credential/callback`.
5. Submit for review.

## Setup
1. Run n8n (Community Edition, Docker):
   ```sh
   docker run -d --name n8n -p 5678:5678 \
     -v n8n_data:/home/node/.n8n \
     -e N8N_SECURE_COOKIE=false \
     n8nio/n8n
   ```
   (Or use n8n Cloud / deploy on the same Render account.)
2. Open n8n → Settings → Community nodes → install **`n8n-nodes-pin-interest`**,
   restart.
3. Credentials → "Pin-Interest OAuth2 API": paste the Pinterest app Client ID
   + Client Secret, click "Connect my account", authorize with the Pinterest
   account. This is the credential referenced as "Pinterest Account".
4. Import `pinterest-list-boards.json`, connect the credential, run it, copy
   the numeric Board ID (e.g. `7176181117597285511`).
5. Import `pstore-rss-to-pinterest.json`, connect the credential, paste the
   Board ID into the `BOARD_ID` constant in "Prep Pin Payload".
6. Import `pstore-pin-healthcheck.json`, replace `ADMIN_EMAIL_CHANGE_ME` /
   `ADMIN_PASSWORD_CHANGE_ME` in the "Login" node, create a Telegram bot with
   @BotFather, paste its token into the "pstore alerts bot" credential, set
   `CHAT_ID_CHANGE_ME`, activate.
7. Import `pstore-fanout-social.json`, flip the TOGGLES flags as each channel
   is set up, activate.

## Six-platform router (`pstore-social-router.json`)

Import it, copy the webhook URL from the **Inbound webhook** node, set pstore's
`SOCIAL_WEBHOOK` env var to that URL (Render → pstore → Environment, redeploy).
Every published kit then hits the router and fans out to the platforms whose
flag is `true` in the **Normalize payload** node's `TOGGLES` constant.

Per-platform credentials (each is YOUR own app — no tool ships these):

| Branch | Node(s) | What you must supply |
|---|---|---|
| X / Twitter | `X post` | Paid X developer app (API v2 user context, OAuth 2.0). Free API tier cannot post — an X dev app is ~$100–200/mo. |
| Facebook | `Facebook page photo` | Meta app + long-lived `PAGE_TOKEN_CHANGE_ME` (replace) + numeric page id in the URL. Page publish permission needed. |
| LinkedIn | `LinkedIn post` | LinkedIn app (OAuth) w/ `w_member_social` scope; paste your `urn:li:person:offus_person...` id into `URN_LI_PERSON_CHANGE_ME`. |
| Instagram | `IG create container` + `IG publish` | Instagram Business account connected to a Meta app; `IG_USER_ID_CHANGE_ME` + `IG_TOKEN_CHANGE_ME`. |
| Pinterest | `Pinterest pin` | Approved Pinterest dev app → OAuth2 credential "Pinterest API v5 (OAuth2)" + numeric `BOARD_ID_CHANGE_ME` (from `pinterest-list-boards.json`). |
| Threads | `Threads create` + `Threads publish` | Threads account in a Meta app (Threads API graduated); `THREADS_USER_ID_CHANGE_ME` + `THREADS_TOKEN_CHANGE_ME`. |

Notes:
- The `body` pstore sends already contains the UTM-tracked link, so every branch
  posts `body` verbatim (no double-link). The PNG share card (`image_png`) is
  attached by the visual branches (FB / IG / Pinterest / Threads).
- Instagram + Threads post in **two steps** (create media container, then
  publish) — the router chains them for you; a failed create never publishes.
- **Do not double-post Pinterest**: keep either pstore's native `_post_pinterest`
  / the bulk RSS importer, OR this branch — not all three at once.
- X is the only branch that needs a paid developer app; skip it (`x: false`)
  and use the older Telegram fan-out if you want a zero-approval mirror.

## Behaviour notes
- Only-new dedup lives in n8n workflow static data (guid/link of each item).
  It persists **only after successful scheduled runs** — so if you click
  "Execute workflow" manually after posting has started, the guard starts empty
  and those items are treated as new again. Verify by running once before
  activating, not after.
- Transient Pinterest failures retry 3× (20 s apart), then the failed item is
  skipped so one hiccup never nukes the whole run.
- The health-check workflow logs in with cookies (n8n stores the session) — if
  it ever returns "unauthorized", the session expired; that resets on the next
  run's fresh login.

## ⚠️ Do not double-post
Pinterest's **native RSS importer** is currently connected to `/rss.xml`
("Create Pins in bulk" → board "Deals", up to 200/day). It needs **no** app
approval and already works today.
- Keep the native importer and **skip n8n** until you want out-of-band
  scheduling (or until the native importer is throttled at 200/day).
- If you switch to n8n, **remove the feed from Pinterest's bulk importer
  first** — otherwise every niche is pinned twice.
- Our own app also ships a native posting kit (`/admin` apikeys → Pinterest
  token + `_post_pinterest` in `publish.py`) — same single-source rule applies.