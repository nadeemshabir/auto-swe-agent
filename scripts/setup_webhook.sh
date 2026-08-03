#!/usr/bin/env bash
# Register (or update) the auto-swe-agent webhook on a GitHub repository.
#
#   bash scripts/setup_webhook.sh --repo owner/name --url https://abc123.ngrok-free.app
#   bash scripts/setup_webhook.sh --repo owner/name --list
#   bash scripts/setup_webhook.sh --repo owner/name --ping
#   bash scripts/setup_webhook.sh --repo owner/name --delete
#
# This removes the manual "go into repo settings and configure a webhook" step
# (plan2.md §18 M13, §21 Phase 0).
#
# Idempotent, and specifically designed for ngrok's rotating URLs: if a hook
# already points at some */webhooks/github, its URL is UPDATED in place rather
# than a second hook being created. Restarting ngrok therefore does not pile up
# dead webhooks on the repo.
#
# Requires in the environment or .env:
#   GITHUB_TOKEN           a PAT with 'admin:repo_hook' (or classic 'repo') scope
#   GITHUB_WEBHOOK_SECRET  the HMAC secret; the API verifies every payload
#                          against it and rejects unsigned requests (§9)
#
# Neither value is ever printed.
set -euo pipefail

REPO=""
URL=""
ACTION="setup"
WEBHOOK_PATH="/webhooks/github"
API="${GITHUB_API_URL:-https://api.github.com}"

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \?//'
    exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)   REPO="${2:-}"; shift 2 ;;
        --url)    URL="${2:-}";  shift 2 ;;
        --list)   ACTION="list";   shift ;;
        --delete) ACTION="delete"; shift ;;
        --ping)   ACTION="ping";   shift ;;
        -h|--help) usage 0 ;;
        *) echo "error: unknown argument '$1'" >&2; usage ;;
    esac
done

# ── load .env (without clobbering anything already exported) ─────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/.env" ]]; then
    while IFS= read -r line; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        key="${line%%=*}"; key="${key//[[:space:]]/}"
        [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
        [[ -n "${!key:-}" ]] && continue          # already exported: keep it
        val="${line#*=}"
        export "${key}=${val}"
    done < "$ROOT/.env"
fi

# ── validate ────────────────────────────────────────────────────────────────
[[ -n "$REPO" ]] || { echo "error: --repo owner/name is required" >&2; usage; }
[[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] \
    || { echo "error: --repo must look like 'owner/name', got '$REPO'" >&2; exit 1; }

: "${GITHUB_TOKEN:?GITHUB_TOKEN is not set (needs admin:repo_hook scope)}"

command -v python3 >/dev/null 2>&1 \
    || { echo "error: python3 is required (used to parse the API's JSON)" >&2; exit 1; }

# Validate setup-specific arguments before printing anything, so a usage error
# is not buried under a banner.
if [[ "$ACTION" == "setup" ]]; then
    [[ -n "$URL" ]] || { echo "error: --url https://host is required" >&2; usage; }
    [[ "$URL" =~ ^https:// ]] \
        || { echo "error: --url must be https (GitHub will not send secrets over http)" >&2; exit 1; }
    : "${GITHUB_WEBHOOK_SECRET:?GITHUB_WEBHOOK_SECRET is not set — the API rejects unsigned webhooks (fail-closed, §9)}"
fi

gh_api() {
    # gh_api METHOD PATH [BODY]  ->  prints "HTTP_STATUS<newline>BODY"
    local method="$1" path="$2" body="${3:-}"
    local args=(-sS -w '\n%{http_code}' -X "$method"
                -H "Authorization: Bearer ${GITHUB_TOKEN}"
                -H "Accept: application/vnd.github+json"
                -H "X-GitHub-Api-Version: 2022-11-28")
    [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
    curl "${args[@]}" "${API}${path}"
}

# Takes the response as an ARGUMENT, not on stdin. Piping into this function
# would run it in a subshell, so RESP_* would be set in a child process and the
# caller would still see them as unset — which `set -u` then turns into a fatal
# "unbound variable" at the first use.
split_response() {  # "$(gh_api ...)" -> sets RESP_BODY / RESP_CODE
    local raw="$1"
    RESP_CODE="${raw##*$'\n'}"
    RESP_BODY="${raw%$'\n'*}"
}

fail_on_error() {
    case "$RESP_CODE" in
        2*) return 0 ;;
        401) echo "error: GitHub rejected the token (401). Is GITHUB_TOKEN valid?" >&2 ;;
        403) echo "error: forbidden (403). The token likely lacks 'admin:repo_hook' scope." >&2 ;;
        404) echo "error: not found (404). Either '$REPO' does not exist, or the token" >&2
             echo "       cannot see it / lacks admin rights on it." >&2 ;;
        422) echo "error: GitHub rejected the payload (422):" >&2
             echo "$RESP_BODY" | python3 -c 'import json,sys; print("      ", json.load(sys.stdin).get("message",""))' 2>/dev/null || true ;;
        *)   echo "error: GitHub API returned HTTP $RESP_CODE" >&2 ;;
    esac
    exit 1
}

# ── find an existing agent hook ─────────────────────────────────────────────
# Matches on the webhook PATH, not the full URL, so a rotated ngrok host is
# recognised as the same hook and updated instead of duplicated.
find_hook() {
    split_response "$(gh_api GET "/repos/${REPO}/hooks")"
    fail_on_error
    HOOK_ID="$(echo "$RESP_BODY" | python3 -c "
import json, sys
path = ${WEBHOOK_PATH@Q}
for h in json.load(sys.stdin):
    if (h.get('config') or {}).get('url','').endswith(path):
        print(h['id']); break
")"
    HOOK_URL="$(echo "$RESP_BODY" | python3 -c "
import json, sys
path = ${WEBHOOK_PATH@Q}
for h in json.load(sys.stdin):
    u = (h.get('config') or {}).get('url','')
    if u.endswith(path):
        print(u); break
")"
}

echo "=== auto-swe-agent webhook · ${REPO} ==="
echo

case "$ACTION" in

list)
    split_response "$(gh_api GET "/repos/${REPO}/hooks")"
    fail_on_error
    echo "$RESP_BODY" | python3 -c "
import json, sys
hooks = json.load(sys.stdin)
if not hooks:
    print('  (no webhooks configured)')
for h in hooks:
    c = h.get('config') or {}
    print(f\"  #{h['id']}  {c.get('url','?')}\")
    print(f\"      events={','.join(h.get('events',[]))}  active={h.get('active')}  \"
          f\"secret={'set' if c.get('secret') else 'NOT SET'}\")
    last = h.get('last_response') or {}
    if last.get('code'):
        print(f\"      last delivery: {last.get('code')} {last.get('message','')}\")
"
    ;;

delete)
    find_hook
    [[ -n "$HOOK_ID" ]] || { echo "  no agent webhook found — nothing to delete"; exit 0; }
    split_response "$(gh_api DELETE "/repos/${REPO}/hooks/${HOOK_ID}")"
    fail_on_error
    echo "[ok] deleted webhook #${HOOK_ID} (${HOOK_URL})"
    ;;

ping)
    find_hook
    [[ -n "$HOOK_ID" ]] || { echo "error: no agent webhook found; run without --ping first" >&2; exit 1; }
    split_response "$(gh_api POST "/repos/${REPO}/hooks/${HOOK_ID}/pings")"
    fail_on_error
    echo "[ok] ping sent to webhook #${HOOK_ID}"
    echo "     A 'ping' event is not actionable, so the agent answers 204 — that is success."
    echo "     Check delivery: bash scripts/setup_webhook.sh --repo ${REPO} --list"
    ;;

setup)
    URL="${URL%/}"
    [[ "$URL" == *"$WEBHOOK_PATH" ]] || URL="${URL}${WEBHOOK_PATH}"

    # Only 'issues' events are actionable today (agent/github.py
    # parse_webhook_event). Subscribing to more would just be noise the
    # endpoint answers 204 to. CI-failure recovery (M16) will add check_suite.
    PAYLOAD="$(python3 -c "
import json, os, sys
print(json.dumps({
    'name': 'web',
    'active': True,
    'events': ['issues'],
    'config': {
        'url': sys.argv[1],
        'content_type': 'json',
        'secret': os.environ['GITHUB_WEBHOOK_SECRET'],
        'insecure_ssl': '0',
    },
}))" "$URL")"

    find_hook
    if [[ -n "$HOOK_ID" ]]; then
        split_response "$(gh_api PATCH "/repos/${REPO}/hooks/${HOOK_ID}" "$PAYLOAD")"
        fail_on_error
        if [[ "$HOOK_URL" == "$URL" ]]; then
            echo "[ok] webhook #${HOOK_ID} already pointed here — refreshed secret and events"
        else
            echo "[ok] webhook #${HOOK_ID} updated"
            echo "       was: ${HOOK_URL}"
            echo "       now: ${URL}"
        fi
    else
        split_response "$(gh_api POST "/repos/${REPO}/hooks" "$PAYLOAD")"
        fail_on_error
        HOOK_ID="$(echo "$RESP_BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
        echo "[ok] webhook #${HOOK_ID} created -> ${URL}"
    fi

    echo "[ok] events: issues   content-type: json   secret: configured"
    echo
    echo "Next:"
    echo "  1. Make sure the API is reachable at that URL (uvicorn/compose + tunnel up)."
    echo "  2. Verify delivery:  bash scripts/setup_webhook.sh --repo ${REPO} --ping"
    echo "  3. Open an issue on ${REPO} and watch the worker log."
    ;;
esac
