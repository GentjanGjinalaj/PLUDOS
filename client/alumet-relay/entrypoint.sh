#!/usr/bin/env bash
# ADR-011 Phase 2 — Jetson alumet-relay entrypoint.
#
# Always active: jetson (INA3221) + prometheus-exporter (local scrape) + csv (local file).
#
# InfluxDB output mode — mutually exclusive to avoid duplicate data:
#   ALUMET_SERVER_ADDR set   → relay-client mode: gRPC to server alumet relay-server,
#                              which writes to InfluxDB on the server side.
#   ALUMET_SERVER_ADDR unset, INFLUXDB_TOKEN set
#                            → direct mode: Jetson influxdb plugin writes straight to
#                              server InfluxDB over HTTP (no gRPC hop, no Tailscale needed).
#   neither set              → local only: Prometheus + CSV, no InfluxDB at all.
#
# Switching modes requires only a .env change — no image rebuild.

set -eo pipefail

CONFIG=/tmp/alumet-config.toml

# Log dir bind-mounted from host (compose.yaml: ./logs/alumet:/app/logs).
# Tee lets podman logs still capture output while also writing a persistent file.
LOG_DIR="${ALUMET_LOG_DIR:-/app/logs}"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/alumet-$(date '+%Y%m%d_%H%M%S').log"

# Build TOML config from confirmed canonical schema (alumet-agent config regen).
# All sections are always written; which plugins activate is controlled via --plugins.
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

# Direct InfluxDB push — used in standalone mode (no relay server).
[plugins.influxdb]
host          = "${INFLUXDB_URL:-http://localhost:8086}"
token         = "${INFLUXDB_TOKEN:-}"
org           = "${INFLUXDB_ORG:-pludos}"
bucket        = "${INFLUXDB_BUCKET:-alumet_energy}"
attributes_as = "field"

# CSV output — written to the bind-mounted logs dir, inspectable on the host.
# Columns: timestamp; metric (with unit); channel label; value.
# Semicolon delimiter avoids collision with decimal commas in French locales.
[plugins.csv]
output_path              = "${LOG_DIR}/alumet_readings.csv"
force_flush              = true
append_unit_to_metric_name = true
use_unit_display_name    = true
csv_delimiter            = ";"
csv_late_delimiter       = ","

# gRPC relay — used when ALUMET_SERVER_ADDR is set.
# server alumet relay-server receives and writes to InfluxDB on the server side.
[plugins.relay-client]
client_name        = "${JETSON_HOSTNAME:-jetson}"
relay_server       = "${ALUMET_SERVER_ADDR:-localhost:50051}"
buffer_max_length  = 4096
buffer_timeout     = "30s"

[plugins.relay-client.retry]
max_times     = 8
initial_delay = "500ms"
max_delay     = "4s"
TOML

# Plugin selection — always: jetson + prometheus-exporter + csv.
# InfluxDB output: relay-client XOR influxdb, never both (avoids duplicate data).
PLUGINS="jetson,prometheus-exporter,csv"
if [ -n "${ALUMET_SERVER_ADDR:-}" ]; then
    # Relay mode: server alumet handles InfluxDB — Jetson does not push directly.
    PLUGINS="${PLUGINS},relay-client"
    echo "[ALUMET-RELAY] mode=relay plugins=${PLUGINS} → ${ALUMET_SERVER_ADDR}"
elif [ -n "${INFLUXDB_TOKEN:-}" ]; then
    # Standalone mode: Jetson pushes directly to InfluxDB (no server alumet needed).
    PLUGINS="${PLUGINS},influxdb"
    echo "[ALUMET-RELAY] mode=direct plugins=${PLUGINS} → ${INFLUXDB_URL:-http://localhost:8086}"
else
    echo "[ALUMET-RELAY] mode=local plugins=${PLUGINS} (no InfluxDB output)"
fi
echo "[ALUMET-RELAY] prometheus=:${ALUMET_PROMETHEUS_PORT:-9095} csv=${LOG_DIR}/alumet_readings.csv"

# Tee to log file; pipefail propagates alumet-agent's exit code through the pipe.
alumet-agent --config "${CONFIG}" --plugins "${PLUGINS}" 2>&1 | tee "${LOG_FILE}"
