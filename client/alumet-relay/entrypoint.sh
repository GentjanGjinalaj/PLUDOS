#!/usr/bin/env bash
# ADR-011 Phase 2 relay container entrypoint.
#
# Starts alumet-cli with the native Jetson INA3221 plugin.
# Relay flags confirmed from Alumet docs:
#   --relay-out <addr>  : forward metric stream to a central alumet server
#
# Two modes, selected by ALUMET_SERVER_ADDR:
#
#   Relay mode  (ALUMET_SERVER_ADDR set):
#     alumet-cli reads INA3221 via the Jetson plugin and forwards the raw
#     metric stream to the server Alumet instance over the Tailscale VPN.
#     The server instance writes everything to InfluxDB.
#
#   Local mode  (ALUMET_SERVER_ADDR unset):
#     alumet-cli reads INA3221 and writes directly to the local/server
#     InfluxDB instance.  Useful when Tailscale is not available.
#
# NOTE: --plugin jetson may need to be --plugin nvidia-jetson depending
# on the installed alumet-cli version.  Verify with `alumet-cli --help`
# after the first build on Jetson hardware.

set -e

if [ -n "${ALUMET_SERVER_ADDR}" ]; then
    echo "[RELAY] Starting alumet-cli relay → ${ALUMET_SERVER_ADDR}"
    exec alumet-cli \
        --plugin jetson \
        --relay-out "${ALUMET_SERVER_ADDR}" \
        --tag "device=${HOSTNAME:-jetson}"
else
    echo "[RELAY] ALUMET_SERVER_ADDR not set — writing directly to InfluxDB"
    exec alumet-cli \
        --plugin jetson \
        --output influxdb \
        --influxdb-url "${INFLUXDB_URL:-http://localhost:8086}" \
        --influxdb-token "${INFLUXDB_TOKEN}" \
        --influxdb-org "${INFLUXDB_ORG:-pludos}" \
        --influxdb-bucket "${INFLUXDB_BUCKET:-alumet_energy}" \
        --tag "device=${HOSTNAME:-jetson}"
fi
