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

set -eo pipefail

CONFIG=/tmp/alumet-config.toml

# Log dir bind-mounted from host (compose.yaml: ./logs/alumet:/app/logs).
# Tee lets podman logs still capture output while also writing a persistent file.
LOG_DIR="${ALUMET_LOG_DIR:-/app/logs}"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/alumet-$(date '+%Y%m%d_%H%M%S').log"

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

# CSV output — written to the bind-mounted logs dir so it's inspectable on the host.
# Columns: timestamp, metric name (with unit), channel label, value.
# Delimiter is semicolon to avoid collision with decimal commas in French locales.
[plugins.csv]
output_path              = "${LOG_DIR}/alumet_readings.csv"
force_flush              = true
append_unit_to_metric_name = true
use_unit_display_name    = true
csv_delimiter            = ";"
csv_late_delimiter       = ","
TOML

# Build plugin list: always jetson + prometheus-exporter + csv, optionally influxdb/relay-client.
PLUGINS="jetson,prometheus-exporter,csv"
[ -n "${INFLUXDB_TOKEN:-}" ]     && PLUGINS="${PLUGINS},influxdb"
[ -n "${ALUMET_SERVER_ADDR:-}" ] && PLUGINS="${PLUGINS},relay-client"

echo "[ALUMET-RELAY] plugins=${PLUGINS} prometheus=:${ALUMET_PROMETHEUS_PORT:-9095}"
[ -n "${INFLUXDB_TOKEN:-}" ]     && echo "[ALUMET-RELAY] InfluxDB → ${INFLUXDB_URL:-http://localhost:8086}"
[ -n "${ALUMET_SERVER_ADDR:-}" ] && echo "[ALUMET-RELAY] relay → ${ALUMET_SERVER_ADDR}"

# Tee to log file; pipefail propagates alumet-agent's exit code through the pipe.
alumet-agent --config "${CONFIG}" --plugins "${PLUGINS}" 2>&1 | tee "${LOG_FILE}"
