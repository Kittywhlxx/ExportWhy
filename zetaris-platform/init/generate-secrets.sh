#!/usr/bin/env bash
#
# First-boot preparation for the shared volumes. Idempotent: safe on every `compose up`.
#
# Three jobs:
#
#   1. Own the volumes. A new named volume is root:root unless its mount path exists in
#      the image, so the unprivileged service users cannot write to it. This runs as root
#      (the only service that does) and hands the trees to the driver's uid.
#
#   2. Generate the field-encryption keypair and the ndp token, ONCE.
#
#   3. Mint the install id, ONCE.
#
# The keypair is generated here, once, rather than by each component separately. The API
# encrypts stored datasource credentials with the public key and the query engine decrypts
# with the private one, so the two must hold the same pair. BACK IT UP: lose it and every
# stored datasource credential becomes unreadable.

set -euo pipefail

DRIVER_UID=1001
DRIVER_GID=1001
DRIVER_DATA=/home/zetaris/zetaris-ndp-driver/data
KEY_DIR="${DRIVER_DATA}/passwordkeys"
TOKEN_DIR="${DRIVER_DATA}/tokens"
INSTALL_ID_FILE="${DRIVER_DATA}/install-id"
CUSTOMER_DATA=/home/zetaris/customer-data
# The driver writes its logs here; the directory must exist and be writable before start.
DRIVER_LOGS=/home/zetaris/zetaris-ndp-driver/logs

log() { printf '[init] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Volume ownership. A new volume belongs to root, so the directories are created and handed
# to the service account before anything else starts.
# ---------------------------------------------------------------------------
mkdir -p "$KEY_DIR" "$TOKEN_DIR" "$CUSTOMER_DATA/shared" "$DRIVER_LOGS"
chown -R "${DRIVER_UID}:${DRIVER_GID}" "$DRIVER_DATA" "$CUSTOMER_DATA" "$DRIVER_LOGS"
chmod 2775 "$CUSTOMER_DATA" "$CUSTOMER_DATA/shared"
chmod -R g+w "$CUSTOMER_DATA"
chmod 0750 "$KEY_DIR" "$TOKEN_DIR"
log "volume ownership set to ${DRIVER_UID}:${DRIVER_GID}"

# ---------------------------------------------------------------------------
# Field-encryption keypair.
# ---------------------------------------------------------------------------
if [ ! -s "${KEY_DIR}/private_key.der" ] || [ ! -s "${KEY_DIR}/public_key.der" ]; then
    log "generating RSA keypair for field encryption"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    openssl genpkey -algorithm RSA -out "$tmp/private_key.pem" -pkeyopt rsa_keygen_bits:2048 2>/dev/null
    openssl pkcs8 -topk8 -inform PEM -outform DER \
        -in "$tmp/private_key.pem" -out "${KEY_DIR}/private_key.der" -nocrypt
    openssl rsa -in "$tmp/private_key.pem" -pubout -out "$tmp/public_key.pem" 2>/dev/null
    openssl rsa -pubin -in "$tmp/public_key.pem" -outform DER \
        -out "${KEY_DIR}/public_key.der" 2>/dev/null
    # 0640: readable by the service group, so the API can read it too. Never world-readable.
    chown "${DRIVER_UID}:${DRIVER_GID}" "${KEY_DIR}/private_key.der" "${KEY_DIR}/public_key.der"
    chmod 0640 "${KEY_DIR}/private_key.der"
    chmod 0644 "${KEY_DIR}/public_key.der"
else
    log "reusing existing field-encryption keypair"
fi

# ---------------------------------------------------------------------------
# ndp token: authenticates the API to the driver over JDBC.
# ---------------------------------------------------------------------------
TOKEN_FILE="${TOKEN_DIR}/ndp_token"
if [ ! -s "$TOKEN_FILE" ]; then
    log "generating ndp token"
    # 32 hex characters, matching the format the driver expects.
    openssl rand -hex 16 > "$TOKEN_FILE"
    chown "${DRIVER_UID}:${DRIVER_GID}" "$TOKEN_FILE"
    chmod 0640 "$TOKEN_FILE"
else
    log "reusing existing ndp token"
fi

# ---------------------------------------------------------------------------
# Install id: a random name this installation can report itself by, so we can
# count how many of these are running without knowing whose they are. Not a
# secret and not a credential -- world-readable, and the API only reads it.
#
# It lives in the data volume, so it survives `compose down` and restarts but a
# `compose down -v` gives you a new one. That is the intent: wiping the volumes
# is a new installation.
# ---------------------------------------------------------------------------
if [ ! -s "$INSTALL_ID_FILE" ]; then
    log "minting install id"
    if [ -r /proc/sys/kernel/random/uuid ]; then
        cat /proc/sys/kernel/random/uuid > "$INSTALL_ID_FILE"
    else
        # Format 16 random bytes as a v4 UUID by hand. Reached on a kernel
        # without the uuid node; openssl is already a dependency above.
        openssl rand -hex 16 \
            | sed -E 's/^(.{8})(.{4}).(.{3}).(.{3})(.{12})$/\1-\2-4\3-a\4-\5/' \
            > "$INSTALL_ID_FILE"
    fi
    chown "${DRIVER_UID}:${DRIVER_GID}" "$INSTALL_ID_FILE"
    chmod 0644 "$INSTALL_ID_FILE"
else
    log "reusing existing install id"
fi

log "done"
