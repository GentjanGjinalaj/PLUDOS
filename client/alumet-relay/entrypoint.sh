#!/usr/bin/env bash
# ADR-011 Phase 2 relay container entrypoint.
#
# Runs alumet-agent with the native Jetson nvidia-jetson plugin (INA3221).
# Binary name is alumet-agent (workspace crate: agent/); not alumet-cli.
#
# Two modes, selected by ALUMET_SERVER_ADDR:
#
#   Relay mode  (ALUMET_SERVER_ADDR set):
#     alumet-agent reads INA3221 via nvidia-jetson plugin and forwards the
#     metric stream to the server Alumet instance over the Tailscale VPN.
#
#   Local mode  (ALUMET_SERVER_ADDR unset):
#     alumet-agent reads INA3221 and writes directly to InfluxDB.
#     Useful when Tailscale is not available (same-network dev runs).
#
# NOTE: exact CLI flags (--plugin, --relay-out, --output) must be verified
# with `alumet-agent --help` after the first hardware build. Update this
# file if flag names differ from what is shown here.

set -e

if [ -n "${ALUMET_SERVER_ADDR}" ]; then
    echo "[RELAY] Starting alumet-agent relay → ${ALUMET_SERVER_ADDR}"
    exec alumet-agent \
        --plugin nvidia-jetson \
        --relay-out "${ALUMET_SERVER_ADDR}" \
        --tag "device=${HOSTNAME:-jetson}"
else
    echo "[LOCAL] ALUMET_SERVER_ADDR not set — writing directly to InfluxDB"
    exec alumet-agent \
        --plugin nvidia-jetson \
        --output influxdb \
        --influxdb-url "${INFLUXDB_URL:-http://localhost:8086}" \
        --influxdb-token "${INFLUXDB_TOKEN}" \
        --influxdb-org "${INFLUXDB_ORG:-pludos}" \
        --influxdb-bucket "${INFLUXDB_BUCKET:-alumet_energy}" \
        --tag "device=${HOSTNAME:-jetson}"
fi
