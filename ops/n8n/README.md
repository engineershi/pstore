# n8n: Pstore RSS → Pinterest

Watches `https://pstore-gxbv.onrender.com/rss.xml` and pins each new niche to
Pinterest. Zero per-pin cost, unlimited (self-hosted n8n Community Edition).

## Files
- `pstore-rss-to-pinterest.json` — the main workflow (import under
  Workflows → ⋯ → Import). Defaults to every 6 hours, max 3 pins per run
  (Pinterest's own guides suggest 10–25/day max, keep a drip not a burst).
- `pinterest-list-boards.json` — run once to discover the numeric board ID.

## Why n8n (vs Make/Pipedream)
- Make / Pipedream: Pinterest "Create a Pin" is Premium/own-token-walled.
- n8n: self-hosted Community Edition is **free and unlimited**; the
  Pin-Interest node handles OAuth token refresh + board targeting so you never
  bake in an expiring token.

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
6. Activate the workflow.

## Behaviour notes
- Only-new dedup lives in n8n workflow static data (guid/link of each item).
  It persists **only after successful scheduled runs** — so if you click
  "Execute workflow" manually after posting has started, the guard starts empty
  and those items are treated as new again. Verify by running once before
  activating, not after.
- Transient Pinterest failures retry 3× (20 s apart), then the failed item is
  skipped so one hiccup never nukes the whole run.

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

## Files touched / ship state
- Added: `ops/n8n/pstore-rss-to-pinterest.json`, `ops/n8n/pinterest-list-boards.json`, `ops/n8n/README.md`.
- No app code changed; not committed yet.