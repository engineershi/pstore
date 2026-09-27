#!/usr/bin/env bash
# Push to GitHub and deploy to Render, as one verified step.
#
# Why this exists: the service runs with autoDeploy=yes/autoDeployTrigger=commit,
# but that webhook failed silently for a two-day run (commits ce4c632..10e760d
# pushed with no deploy at all). When the webhook is healthy the push alone is
# enough; when it is not, nothing deploys and a push looks like a success. So
# this script waits for Render's own deploy first and only triggers one itself
# if none shows up.
#
# The important part is not the deploy trigger, it is the VERIFICATION at the
# end: the script confirms the deploy that went live is the commit that was
# pushed. A push with no matching deploy used to look like a success.
#
# Usage:
#   deploy/push.sh                 # push, let Render's webhook deploy, verify
#   deploy/push.sh --force-deploy  # also trigger a deploy even if one appears
#   deploy/push.sh --no-deploy     # push only
#   deploy/push.sh --clear-cache   # force a deploy and bust the build cache
#
# Credentials are read from the environment, never stored here:
#   RENDER_API_KEY   (required)
#   RENDER_SERVICE   (optional, defaults to the pstore web service)
# Fallback file: $HOME/.render_api_key, mode 600, outside this repo.

set -euo pipefail

SERVICE_DEFAULT="srv-da9icj942hec7380soi0"
API="https://api.render.com/v1"
# How long to let Render's own webhook deploy show up before triggering one.
WEBHOOK_WAIT="${WEBHOOK_WAIT:-90}"

DO_DEPLOY=1
FORCE_DEPLOY=0
CLEAR_CACHE=""
for arg in "$@"; do
  case "$arg" in
    --no-deploy)    DO_DEPLOY=0 ;;
    --force-deploy) FORCE_DEPLOY=1 ;;
    --clear-cache)  FORCE_DEPLOY=1; CLEAR_CACHE='"clearCache": "clear",' ;;
    -h|--help)      sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }

# ---- credentials -----------------------------------------------------------
if [ -z "${RENDER_API_KEY:-}" ]; then
  KEYFILE="${HOME}/.render_api_key"
  [ -r "$KEYFILE" ] || die "no RENDER_API_KEY set and $KEYFILE is unreadable"
  # shellcheck disable=SC1090
  . "$KEYFILE"
fi
[ -n "${RENDER_API_KEY:-}" ] || die "RENDER_API_KEY is empty"
SERVICE="${RENDER_SERVICE:-$SERVICE_DEFAULT}"

api() { # api <method> <path> [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" -H "Authorization: Bearer $RENDER_API_KEY" \
         -H "Accept: application/json" -H "Content-Type: application/json" \
         -d "$body" "$API$path"
  else
    curl -sS -X "$method" -H "Authorization: Bearer $RENDER_API_KEY" \
         -H "Accept: application/json" "$API$path"
  fi
}

json_get() { # json_get <json> <python-expression on `d`>
  python3 -c "import json,sys;d=json.loads(sys.stdin.read() or '{}');print($1)" <<<"$2" 2>/dev/null
}

# Newest commit-triggered deploy created at/after the given ISO cutoff, or "".
# The deploy list carries createdAt and trigger but not the commit sha, so
# recency plus trigger is what identifies "the deploy caused by our push".
find_own_deploy() { # find_own_deploy <cutoff-iso>
  api GET "/services/$SERVICE/deploys?limit=5" | python3 -c '
import json, sys
cut = sys.argv[1]
try:
    rows = json.load(sys.stdin)
except Exception:
    rows = []
best = None
for row in rows or []:
    d = row.get("deploy", row) if isinstance(row, dict) else {}
    if not isinstance(d, dict):
        continue
    if d.get("trigger") != "new_commit":
        continue
    if (d.get("createdAt") or "") < cut:
        continue
    if best is None or (d.get("createdAt") or "") > (best.get("createdAt") or ""):
        best = d
print((best or {}).get("id", ""))
' "$1" 2>/dev/null || true
}

# ---- preflight -------------------------------------------------------------
cd "$(git rev-parse --show-toplevel)"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
HEAD_SHA="$(git rev-parse HEAD)"
HEAD_SHORT="${HEAD_SHA:0:7}"

if [ -n "$(git status --porcelain)" ]; then
  die "working tree is dirty -- commit or stash first:
$(git status --porcelain | head -20)"
fi

git fetch -q origin "$BRANCH"
# left = commits on origin we do not have, right = commits we have to push.
# Being ahead is the normal case. Being behind or diverged means someone else
# moved the branch, and pushing would either fail or clobber their work.
read -r BEHIND AHEAD <<<"$(git rev-list --left-right --count "origin/$BRANCH...HEAD")"
if [ "${BEHIND:-0}" -gt 0 ]; then
  if [ "${AHEAD:-0}" -gt 0 ]; then
    die "branch $BRANCH has diverged from origin ($BEHIND behind, $AHEAD ahead) -- rebase or merge first"
  fi
  die "origin/$BRANCH is $BEHIND commit(s) ahead of local $BRANCH -- pull first"
fi
[ "${AHEAD:-0}" -gt 0 ] && say "$AHEAD commit(s) to push to origin/$BRANCH"

say "pushing $BRANCH ($HEAD_SHORT $(git log -1 --format=%s))"
PUSH_EPOCH="$(date +%s)"
git push origin "$BRANCH"

[ "$DO_DEPLOY" -eq 1 ] || { say "pushed; --no-deploy given, stopping here"; exit 0; }

# ---- trigger ---------------------------------------------------------------
# autoDeployTrigger=commit means the push above normally starts a deploy on its
# own. Triggering another one would build and roll out the same commit twice,
# so look for Render's deploy first and only create one if none arrives.
PUSH_CUTOFF="$(date -u -d "@$PUSH_EPOCH" +%Y-%m-%dT%H:%M:%S)"
DEP_ID=""
DEP_TRIGGER=""

if [ "$FORCE_DEPLOY" -eq 0 ]; then
  say "waiting up to ${WEBHOOK_WAIT}s for Render's own deploy of $HEAD_SHORT"
  WAIT_UNTIL=$(( $(date +%s) + WEBHOOK_WAIT ))
  while :; do
    DEP_ID="$(find_own_deploy "$PUSH_CUTOFF")"
    [ -z "$DEP_ID" ] || { DEP_TRIGGER="new_commit"; break; }
    [ "$(date +%s)" -lt "$WAIT_UNTIL" ] || break
    printf '.'
    sleep 5
  done
  printf '\n'
  if [ -n "$DEP_ID" ]; then
    say "Render started deploy $DEP_ID on its own; not triggering a duplicate"
  else
    say "no deploy from the webhook in ${WEBHOOK_WAIT}s -- triggering one"
  fi
fi

if [ -z "$DEP_ID" ]; then
  say "triggering deploy for $SERVICE"
  if [ -n "$CLEAR_CACHE" ]; then
    RESP="$(api POST "/services/$SERVICE/deploys" '{"clearCache":"clear"}')"
  else
    RESP="$(api POST "/services/$SERVICE/deploys" '{}')"
  fi
  DEP_ID="$(json_get 'd.get("id","")' "$RESP")"
  DEP_COMMIT="$(json_get '(d.get("commit") or {}).get("id","")' "$RESP")"
  DEP_TRIGGER="api"
  [ -n "$DEP_ID" ] || die "Render did not return a deploy id: $RESP"

  if [ -n "$DEP_COMMIT" ] && [ "${DEP_COMMIT:0:7}" != "$HEAD_SHORT" ]; then
    die "Render built $DEP_COMMIT but we pushed $HEAD_SHA -- aborting before rollout"
  fi
fi
say "following deploy $DEP_ID (trigger=$DEP_TRIGGER)"

# ---- wait ------------------------------------------------------------------
DEADLINE=$(( $(date +%s) + 900 ))
STATUS=""
while :; do
  D="$(api GET "/services/$SERVICE/deploys/$DEP_ID")"
  STATUS="$(json_get 'd.get("status","")' "$D")"
  BUILT="$(json_get '(d.get("commit") or {}).get("id","")' "$D")"
  printf '    %s  %s\n' "$(date -u +%T)" "${STATUS:-unknown}"
  case "$STATUS" in
    live) break ;;
    build_failed|canceled|deactivated)
      die "deploy $DEP_ID ended '$STATUS'. Render build log: dashboard.render.com/web/$SERVICE" ;;
  esac
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    die "timed out after 15m with status '$STATUS' (may still be deploying)"
  fi
  sleep 15
done

# ---- verify ----------------------------------------------------------------
if [ -n "$BUILT" ] && [ "$BUILT" != "$HEAD_SHA" ]; then
  die "live commit $BUILT != pushed $HEAD_SHA"
fi

say "LIVE: $HEAD_SHORT is serving"
cat <<EOF

Next: verify behaviour, not just the build. A green build is not a working site.
  - the pages you changed actually render the way you changed them
  - the persistent DB survived: /api/niches total must match pre-deploy
  - no background job stuck in running=true
  - for a data change, one authenticated read of the new value through the API
EOF
