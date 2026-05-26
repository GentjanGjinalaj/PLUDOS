#!/usr/bin/env bash
# ADR-011 Phase 2 — Jetson alumet-relay entrypoint.
#
# Always starts: jetson (INA3221 reader) + prometheus-exporter (local HTTP scrape).
# Optionally adds: influxdb if INFLUXDB_TOKEN is set (push to Grafana server).
#                  relay-client if ALUMET_SERVER_ADDR is set (gRPC forward to server Alumet).
#
# client.py AlumetProfiler scrapes ALUMET_PROMETHEUS_URL (default localhost:9095/metrics)
# for real-time power data — no Tailscale dependency for local energy measurement.
# Data also flows to InfluxDB (alumet_energy bucket) for Grafana dashboards.
#
# Plugin names from alumet v0.9.4 (confirmed via `alumet-agent plugins list`):
#   jetson              — INA3221 power rails (nvidia-jetson plugin)
#   prometheus-exporter — local HTTP metrics on ALUMET_PROMETHEUS_PORT (default 9095)
#   influxdb            — InfluxDB v2 push output
#   relay-client        — gRPC forward to server Alumet relay-server

set -e

CONFIG=/tmp/alumet-config.toml

# Build config from confirmed canonical schema (alumet-agent config regen).
# prometheus-exporter and jetson are always included.
# influxdb section is always written; the plugin is conditionally enabled below.
cat > "${CONFIG}" <<TOML
[plugins.jetson]
poll_interval  = "5s"
flush_interval = "5s"

[plugins.prometheus-exporter]
host                     = "0.0.0.0"
port                     = ${ALUMET_PROMETHEUS_PORT:-9095}
prefix                   = ""
suffix                   = "_alumet"
add_attributes_to_labels = true

[plugins.influxdb]
host   = "${INFLUXDB_URL:-http://localhost:8086}"
token  = "${INFLUXDB_TOKEN:-}"
org    = "${INFLUXDB_ORG:-pludos}"
bucket = "${INFLUXDB_BUCKET:-alumet_energy}"
attributes_as = "field"
TOML

# Build plugin list: always jetson + prometheus-exporter, optionally influxdb/relay-client.
PLUGINS="jetson,prometheus-exporter"
[ -n "${INFLUXDB_TOKEN:-}" ]     && PLUGINS="${PLUGINS},influxdb"
[ -n "${ALUMET_SERVER_ADDR:-}" ] && PLUGINS="${PLUGINS},relay-client"

echo "[ALUMET-RELAY] plugins=${PLUGINS} prometheus=:${ALUMET_PROMETHEUS_PORT:-9095}"
[ -n "${INFLUXDB_TOKEN:-}" ]     && echo "[ALUMET-RELAY] InfluxDB → ${INFLUXDB_URL:-http://localhost:8086}"
[ -n "${ALUMET_SERVER_ADDR:-}" ] && echo "[ALUMET-RELAY] relay → ${ALUMET_SERVER_ADDR}"

exec alumet-agent --config "${CONFIG}" --plugins "${PLUGINS}"
