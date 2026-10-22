#!/usr/bin/env bash
#
# Check a running platform is working, and say what to do when it is not.
#
# This is read-only and safe against an install you care about. The other suite,
# verify-install.sh, builds a throwaway stack on empty volumes and destroys them
# afterwards; never point that one at a real install.
#
# Usage: ./verify.sh
# Exits non-zero if any check fails.

set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

pass=0; fail=0
# the driver creates the organisation and its administrator on first start, so until it
# is up there is no account to log in as. A login failure before then means "not ready yet", not "wrong
# password", and the two need different advice.
BOOTSTRAP_PENDING=0
ok()   { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail+1)); }
hint() { printf '        -> %s\n' "$1"; }

DC="docker compose"
$DC version >/dev/null 2>&1 || { printf 'ERROR: the docker compose plugin is missing. Run ./preflight.sh\n' >&2; exit 1; }

printf '%s\n' "Zetaris platform -- verify"
printf '%s\n' "=========================="
printf '\n'

# --- .env ---------------------------------------------------------------------------------
# Follow COMPOSE_ENV_FILES the way compose itself does, rather than assuming .env. Reading
# credentials from a different file than the stack was started with sends the wrong password
# and reports a healthy platform as a failed login.
ENV_FILE="${COMPOSE_ENV_FILES:-.env}"
ENV_FILE="${ENV_FILE%%,*}"          # COMPOSE_ENV_FILES may list several; the first wins
if [ ! -f "$ENV_FILE" ]; then
    printf 'ERROR: no %s here. Run ./env/make-env.sh first.\n' "$ENV_FILE" >&2
    exit 1
fi
# Read only the two values needed to log in. Sourcing the whole file would execute it.
ADMIN_EMAIL=$(sed -n 's/^ZETARIS_ADMIN_EMAIL=//p' "$ENV_FILE" | tail -1)
ADMIN_PW=$(sed -n 's/^ZETARIS_ADMIN_PASSWORD=//p' "$ENV_FILE" | tail -1)
: "${ADMIN_EMAIL:=admin@zetaris.com}"

# --- containers ---------------------------------------------------------------------------
# Long-running services. A container that was never created and one that crashed need
# different advice.
for svc in postgres driver api ui; do
    cid=$($DC ps -q "$svc" 2>/dev/null | head -1)
    if [ -z "$cid" ]; then
        bad "${svc}: not running"
        hint "start the stack with: docker compose up -d"
        continue
    fi
    state=$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null)
    if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || [ "$health" = "none" ]; }; then
        ok "${svc}: running${health:+ (${health})}"
    else
        bad "${svc}: ${state}${health:+/${health}}"
        hint "see why with: docker compose logs --tail=50 ${svc}"
        [ "$svc" = "driver" ] && hint "a driver that exits on startup is usually out of memory -- run ./preflight.sh"
    fi
done

# --- one-shot jobs ------------------------------------------------------------------------
# init and bootstrap are expected to end in Exited(0), which reads like a crash if you are
# not expecting it. Say so explicitly.
for svc in init bootstrap; do
    cid=$($DC ps -aq "$svc" 2>/dev/null | head -1)
    if [ -z "$cid" ]; then
        bad "${svc}: never ran"
        continue
    fi
    code=$(docker inspect -f '{{.State.ExitCode}}' "$cid" 2>/dev/null)
    state=$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null)
    if [ "$state" = "running" ]; then
        bad "${svc}: still running (it is a one-shot job and should have finished)"
                [ "$svc" = "bootstrap" ] && hint "on a slow machine this takes several minutes -- it loads the sample data"
    elif [ "$code" = "0" ]; then
        ok "${svc}: completed (exit 0 -- this is correct for a one-shot job)"
    else
        bad "${svc}: exited ${code}"
        hint "docker compose logs ${svc}"
    fi
done

# --- probes inside the network ------------------------------------------------------------
# Run these from inside the api container rather than the host. No published port is
# involved, so nothing else listening on 3000 or 8888 can answer instead.
API_C=$($DC ps -q api 2>/dev/null | head -1)
ncurl() { docker exec "$API_C" curl -s --max-time 30 "$@" 2>/dev/null; }
ncode() { docker exec "$API_C" curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@" 2>/dev/null; }
jwt()   { grep -oE '[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}' | head -1; }

if [ -n "$API_C" ]; then
    code=$(ncode "http://ui:3000/api/config")
    [ "$code" = "200" ] && ok "ui responding (/api/config 200)" || {
        bad "ui not responding (/api/config returned '${code:-nothing}')"
        hint "docker compose logs --tail=50 ui"
    }

    # Keep the whole response: it carries the organisation id as well as the token,
    # and the query below needs both. The id is NOT always 1 -- it comes from a
    # sequence shared with every other row created during bootstrap, so a database
    # that is not pristine (a retried install, or one kept from an earlier attempt)
    # lands on a different number, and a hardcoded 1 then fails the query on a
    # platform that is working.
    LOGIN=$(ncurl -X POST "http://ui:3000/api/auth/login" -H 'Content-Type: application/json' \
             -d "$(printf '{"username":"%s","password":"%s"}' "$ADMIN_EMAIL" "$ADMIN_PW")")
    TOKEN=$(printf '%s' "$LOGIN" | jwt)
    ORG_ID=$(printf '%s' "$LOGIN" | grep -oE '"orgId"[[:space:]]*:[[:space:]]*[0-9]+' | grep -oE '[0-9]+$')
    if [ -n "$TOKEN" ]; then
        ok "login as ${ADMIN_EMAIL}"
    else
        bad "login as ${ADMIN_EMAIL} returned no token"
        if [ "$BOOTSTRAP_PENDING" -eq 1 ]; then
            hint "the driver has not finished creating the account yet -- wait, then re-run ./verify.sh"
            hint "watch it with: docker compose logs -f driver"
        else
            hint "check ZETARIS_ADMIN_PASSWORD in ${ENV_FILE} matches what bootstrap created"
            hint "docker compose logs driver | grep -i organisation"
        fi
    fi

    # Everything above can pass while the API cannot reach the driver. This is the check
    # that covers that.
    if [ -n "$TOKEN" ] && [ -z "$ORG_ID" ]; then
        # Sending the header empty comes back as an authorisation failure, which
        # reads as a broken platform. Say what actually went wrong instead.
        bad "query skipped (login succeeded but returned no orgId)"
        hint "the login response should carry orgId alongside the tokens -- check the api version"
    elif [ -n "$TOKEN" ]; then
        r=$(ncurl -X POST "http://ui:3000/api/proxy/query/sql/start" -H 'Content-Type: application/json' \
            -H "Authorization: Bearer $TOKEN" -H "X-Org-ID: ${ORG_ID}" \
            -H "X-Request-ID: 11111111-2222-3333-4444-555555555555" \
            -d '{"select":"select 7 as seven","pageLimit":5}')
        if printf '%s' "$r" | grep -q '"seven":"7"'; then
            ok "query through ui -> api -> driver -> spark"
        else
            bad "query failed: $(printf '%s' "$r" | head -c 160)"
            hint "this is usually the driver still starting -- wait a minute and retry"
            hint "docker compose logs --tail=50 driver"
        fi
    else
        bad "query skipped (no login token)"
    fi
else
    bad "cannot probe the platform: the api container is not running"
fi

# --- summary ------------------------------------------------------------------------------
printf '\n'
if [ "$fail" -gt 0 ]; then
    printf '%d of %d checks failed.\n' "$fail" "$((pass+fail))"
    printf 'Quote the FAIL lines above when asking for help.\n'
    exit 1
fi
printf 'All %d checks passed. The platform is ready on http://localhost:3000\n' "$pass"
printf 'Sign in as %s\n' "$ADMIN_EMAIL"
exit 0
