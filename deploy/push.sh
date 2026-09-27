#!/usr/bin/env bash
# Push to GitHub and deploy to Render, as one verified step.
#
# Why this exists: the service has autoDeploy=yes/autoDeployTrigger=commit, but
# the GitHub->Render webhook stopped arriving. All 25 deploys in the service's
# history have trigger=api, and a two-day run of pushed commits (ce4c632 through
# 10e760d) produced no deploy at all. This makes the push->deploy path
# deterministic instead of depending on that webhook.
#
# The important part is not the deploy trigger, it is the VERIFICATION at the
# end: the script confirms the deploy that went live is the commit that was
# pushed. A push with no matching deploy used to look like a success.
#
# Usage:
#   deploy/push.sh                 # push master, deploy, wait, verify
#   deploy/push.sh --no-deploy     # push only
#   deploy/push.sh --clear-cache   # also bust the build cache
#
# Credentials are read from the environment, never stored here:
#   RENDER_API_KEY   (required)
#   RENDER_SERVICE   (optional, defaults to the pstore web service)
# Fallback file: $HOME/.render_api_key, mode 600, outside this repo.

set -euo pipefail

SERVICE_DEFAULT="srv-da9icj942hec7380soi0"
API="https://api.render.com/v1"

DO_DEPLOY=1
CLEAR_CACHE=""
for arg in "$@"; do
  case "$arg" in
    --no-deploy)   DO_DEPLOY=0 ;;
    --clear-cache) CLEAR_CACHE='"clearCache": "clear",' ;;
    -h|--help)     sed -n '2,22p' "$0"; exit 0 ;;
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
git push origin "$BRANCH"

[ "$DO_DEPLOY" -eq 1 ] || { say "pushed; --no-deploy given, stopping here"; exit 0; }

# ---- trigger ---------------------------------------------------------------
say "triggering deploy for $SERVICE"
if [ -n "$CLEAR_CACHE" ]; then
  RESP="$(api POST "/services/$SERVICE/deploys" '{"clearCache":"clear"}')"
else
  RESP="$(api POST "/services/$SERVICE/deploys" '{}')"
fi

DEP_ID="$(json_get 'd.get("id","")' "$RESP")"
DEP_COMMIT="$(json_get '(d.get("commit") or {}).get("id","")' "$RESP")"
[ -n "$DEP_ID" ] || die "Render did not return a deploy id: $RESP"

if [ -n "$DEP_COMMIT" ] && [ "${DEP_COMMIT:0:7}" != "$HEAD_SHORT" ]; then
  die "Render built $DEP_COMMIT but we pushed $HEAD_SHA -- aborting before rollout"
fi
say "deploy $DEP_ID on ${DEP_COMMIT:0:7}"

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

Next: verify behaviour, not just the build.
  - /n/best-griddle-pan must contain 0 "best best"
  - /api/niches?limit=3 must return 3 rows + a total
  - a background job must not be stuck in running=true forever
EOF
