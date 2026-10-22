#!/usr/bin/env bash
#
# Register the sample data, once. The organisation and administrator already exist: the
# driver registers them on first start, from the same ZETARIS_ADMIN_* settings.
#
# Runs in the driver image because that is where seed-org.sh lives.

set -euo pipefail

# Anything the seed creates under customer-data must stay group-writable: the API reaches it
# through group 1001 as a different uid.
umask 002

log() { printf '[bootstrap] %s\n' "$*"; }

: "${ZETARIS_ADMIN_EMAIL:?}"
: "${ZETARIS_ADMIN_PASSWORD:?}"

DRIVER_DATA=/home/zetaris/zetaris-ndp-driver/data
# Seeding opens a Lightning session -- a JVM/Spark startup that takes minutes -- so the marker
# keeps a repeat `compose up` from paying that just to find nothing to do. It lives in the same
# volume as the metastore credentials, so `down -v` removes both together. To force the seed
# again -- someone dropped a sample database by hand, say -- delete it:
#
#   docker compose run --rm --entrypoint \
#     "rm -f /home/zetaris/zetaris-ndp-driver/data/.sample-data-registered" bootstrap
MARK_SAMPLE_DATA="${DRIVER_DATA}/.sample-data-registered"
SEED_LOG="${ZETARIS_NDP_DRIVER_HOME}/logs/sample-data-seed.log"

# Credentials go through stdin so they stay out of the process list.
admin_login_status() {
    printf 'user = "%s:%s"\n' "$ZETARIS_ADMIN_EMAIL" "$ZETARIS_ADMIN_PASSWORD" \
      | curl -s -o /dev/null -w '%{http_code}' -m 60 -K - \
          -H "X-Request-ID: $(cat /proc/sys/kernel/random/uuid)" \
          http://api:8889/api/v1.0/internal/auth/legacy-login || true
}

login=$(admin_login_status)
if [ "$login" != 200 ]; then
    log "ERROR: the administrator cannot log in (HTTP ${login:-none})"
    log "       check ZETARIS_ADMIN_PASSWORD matches what the driver registered:"
    log "       docker compose logs driver | grep -i organisation"
    exit 1
fi
log "administrator '${ZETARIS_ADMIN_EMAIL}' can log in"

# The driver image ships no psql, so "does this already exist?" cannot be answered by
# querying the metastore from here. The seed is ATTEMPTED and an "already exists" failure is
# treated as success.
already_exists() { grep -qaiE "already exist|already registered|duplicate" "$1"; }

# seed-org.sh ships in the driver image, so compose and a Zetaris Cloud cluster get identical
# datasets (NY_TAXI and TPCDS_DB, pointer tables over s3a://zetaris-public-dataset) and the same
# instructions work on both. Needs outbound internet to S3. Never fatal: a seeding problem must
# not make a working install look failed.
if [ -f "$MARK_SAMPLE_DATA" ]; then
    log "sample data already registered on a previous start; skipping"
else
    log "registering the sample datasets NY_TAXI and TPCDS_DB"
    # Redirected to a file, NOT piped to tail: a pipe makes the exit status tail's, so a
    # failure reads as success and the diagnostic is discarded.
    if bash "${ZETARIS_NDP_DRIVER_HOME}/bin/seed-org.sh" \
         "$ZETARIS_ADMIN_EMAIL" "$ZETARIS_ADMIN_PASSWORD" > "$SEED_LOG" 2>&1; then
        log "sample data registered"; : > "$MARK_SAMPLE_DATA"
    elif already_exists "$SEED_LOG"; then
        log "sample data already registered; leaving it untouched"; : > "$MARK_SAMPLE_DATA"
    else
        log "WARNING: sample data registration failed; the stack is still usable"
        grep -aE "LightningException|AuthenticationException|UnknownHost|ERROR .*Failed in" "$SEED_LOG" \
          | head -3 | sed 's/^/[bootstrap]   /' || true
        log "  full output: ${SEED_LOG}"
    fi
fi

log "done"
