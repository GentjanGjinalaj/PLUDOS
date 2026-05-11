#!/usr/bin/env bash
# ADR-011 Phase 2 relay container entrypoint.
#
# Two modes, selected by ALUMET_SERVER_ADDR:
#   Relay mode  (set):   jetson plugin → relay-client → server Alumet gRPC
#   Local mode  (unset): jetson plugin → InfluxDB directly
#
# Plugin names confirmed from alumet v0.9.4 source (agent/src/bin/main.rs):
#   "jetson"        — INA3221 reader (plugins/nvidia-jetson)
#   "influxdb"      — InfluxDB v2 output (plugins/influxdb)
#   "relay-client"  — gRPC relay output (plugins/relay)
#
# InfluxDB TOML config keys (plugins/influxdb/README.md): host, token, org, bucket.

set -e

CONFIG=/tmp/alumet-config.toml

if [ -n "${ALUMET_SERVER_ADDR}" ]; then
    echo "[RELAY] Starting alumet-agent relay → ${ALUMET_SERVER_ADDR}"
    # --relay-out is a top-level CLI shortcut for plugins.relay-client.relay_server
    exec alumet-agent \
        --plugins jetson,relay-client \
        --relay-out "${ALUMET_SERVER_ADDR}"
else
    echo "[LOCAL] ALUMET_SERVER_ADDR not set — writing directly to InfluxDB"
    # Write credentials into a temp TOML config; avoids shell quoting issues with --config-override.
    cat > "${CONFIG}" <<TOML
[plugins.influxdb]
host   = "${INFLUXDB_URL:-http://localhost:8086}"
token  = "${INFLUXDB_TOKEN}"
org    = "${INFLUXDB_ORG:-pludos}"
bucket = "${INFLUXDB_BUCKET:-alumet_energy}"
# Serialize channel label as a tag for Grafana grouping; numeric IDs as fields.
attributes_as      = "field"
attributes_as_tags = ["ina_channel_label"]
TOML
    exec alumet-agent \
        --config "${CONFIG}" \
        --plugins jetson,influxdb
fi
