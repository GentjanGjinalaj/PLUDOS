#!/usr/bin/env bash
# ADR-011 Phase 2 server-side Alumet entrypoint.
#
# Plugins (all active simultaneously):
#   rapl               — server CPU energy during FL rounds via /sys/class/powercap
#   relay-server       — receives INA3221 streams from Jetson alumet-relay clients
#   influxdb           — persistent write to InfluxDB (fl_energy + stm_mission)
#   prometheus-exporter — live Grafana scrape on port 9091
#
# Plugin names confirmed from alumet v0.9.4 agent/src/bin/main.rs.
# RAPL requires /sys/class/powercap mounted read-only — see compose.yaml.
# relay-server listens passively; no Jetson clients connected = silent, no error.

set -e

CONFIG=/tmp/alumet-config.toml

# Generate TOML config from environment variables at container start.
cat > "${CONFIG}" <<TOML
[plugins.rapl]
poll_interval    = "1s"
flush_interval   = "5s"
# Required by rapl v0.3.1 — disable perf_events fallback (not needed for RAPL sysfs).
no_perf_events   = true

[plugins.relay-server]
# Accept metric streams from all Jetson alumet-relay sidecars.
address = "0.0.0.0:${ALUMET_RELAY_PORT:-50051}"

[plugins.influxdb]
host   = "${INFLUXDB_URL:-http://localhost:8086}"
token  = "${INFLUXDB_TOKEN}"
org    = "${INFLUXDB_ORG:-pludos}"
bucket = "${INFLUXDB_BUCKET:-alumet_energy}"
# domain and ina_channel_label as tags for per-rail Grafana queries.
attributes_as      = "field"
attributes_as_tags = ["domain", "ina_channel_label"]

[plugins.prometheus-exporter]
host = "0.0.0.0"
port = 9091
TOML

echo "[ALUMET] Starting server agent (rapl + relay-server + influxdb + prometheus-exporter)"
exec alumet-agent \
    --config "${CONFIG}" \
    --plugins rapl,relay-server,influxdb,prometheus-exporter
