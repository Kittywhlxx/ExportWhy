#!/usr/bin/env bash
#
# Driver entrypoint. Writes the settings that only reach the query engine through
# meta_store.properties, then starts it.
#

set -euo pipefail

# Uploads land in directories the query engine creates and the API writes into, as two
# different users sharing a group. 002 rather than the default 022 keeps them group-writable.
umask 002

CONF="${ZETARIS_NDP_DRIVER_HOME}/conf/meta_store.properties"

set_prop() {
    local key=$1 val=$2
    [ -n "$val" ] || return 0
    local esc
    esc=$(printf '%s' "$val" | sed -e 's/[\\&|]/\\&/g')
    if grep -qE "^${key}=" "$CONF" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${esc}|" "$CONF"
    else
        printf '%s=%s\n' "$key" "$val" >> "$CONF"
    fi
}

if [ "${QUERY_HTTP_PORT:-0}" != "0" ] && [ -n "${QUERY_HTTP_PORT:-}" ]; then
    echo "[driver] enabling the Lightning Catalog REST surface on ${QUERY_HTTP_PORT}"
    set_prop query_http_port  "${QUERY_HTTP_PORT}"
    set_prop query_http_bind  "${QUERY_HTTP_BIND:-0.0.0.0}"
    set_prop query_http_token "${QUERY_HTTP_TOKEN:-}"
    set_prop query_http_user  "${QUERY_HTTP_USER:-}"
    set_prop query_http_org   "${QUERY_HTTP_ORG:-}"
    set_prop graph_catalog_name "${GRAPH_CATALOG_NAME:-lightning_graph}"
fi

exec "${ZETARIS_NDP_DRIVER_HOME}/bin/start-zetaris-ndp-driver.sh"
