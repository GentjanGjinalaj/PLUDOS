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

# --- Housekeeping limits (bound unbounded growth on the Jetson eMMC) ---------
# CSV is written by alumet's csv plugin (no built-in rotation in v0.9.4), so it
# is rotated externally inside the watchdog loop below. Per-restart .log files
# accumulate one per container restart and are pruned here at startup.
CSV_MAX_BYTES=$(( ${ALUMET_CSV_MAX_MB:-200} * 1024 * 1024 ))  # rotate live CSV past this size
CSV_KEEP="${ALUMET_CSV_KEEP:-3}"                              # rotated alumet_readings_*.csv to retain
LOG_KEEP="${ALUMET_LOG_KEEP:-5}"                              # per-restart .log files to retain (incl. current)

# --- INA3221 sample cadence ---------------------------------------------------
# Default 200 ms (5 Hz): a multi-MB MOVING drain lasts ~seconds, so 5 Hz lands
# several real power samples per drain for an honest per-drain energy integral; the
# old 1 s default caught at most one. Bounded by the ina3221 hwmon driver's own ADC
# update rate — polling faster than the ADC re-reads stale values, so verify on the
# Jetson before going below ~100 ms. flush_interval matches so points reach InfluxDB
# promptly (a slow flush would batch drains together and blur the per-drain window).
# Higher rate = proportionally more CSV/InfluxDB volume; CSV rotation already caps size.
ALUMET_POLL_INTERVAL="${ALUMET_POLL_INTERVAL:-200ms}"
ALUMET_FLUSH_INTERVAL="${ALUMET_FLUSH_INTERVAL:-200ms}"

# Prune old per-restart logs: keep the newest (LOG_KEEP - 1) so that, once the
# current run's LOG_FILE is created below, at most LOG_KEEP files remain.
find "${LOG_DIR}" -maxdepth 1 -name 'alumet-*.log' -type f -printf '%T@ %p\n' \
    | sort -rn | tail -n +"${LOG_KEEP}" | cut -d' ' -f2- | xargs -r rm -f

# Build TOML config from confirmed canonical schema (alumet-agent config regen).
# All sections are always written; which plugins activate is controlled via --plugins.
cat > "${CONFIG}" <<TOML
[plugins.jetson]
poll_interval  = "${ALUMET_POLL_INTERVAL}"
flush_interval = "${ALUMET_FLUSH_INTERVAL}"

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
echo "[ALUMET-RELAY] ina3221 poll=${ALUMET_POLL_INTERVAL} flush=${ALUMET_FLUSH_INTERVAL}"

# Run agent + tee in background so we can also run a watchdog alongside.
alumet-agent --config "${CONFIG}" --plugins "${PLUGINS}" 2>&1 | tee "${LOG_FILE}" &
TEED_PID=$!

# T7.2 watchdog: exit the container (→ Podman restarts it) if alumet writes
# ALUMET_ZERO_THRESHOLD consecutive zero power readings to the CSV.
# Killing tee (TEED_PID) sends SIGPIPE to alumet-agent; both die; wait returns
# non-zero; Podman's restart: unless-stopped brings the container back up.
ZERO_THRESHOLD="${ALUMET_ZERO_THRESHOLD:-5}"
(
    zeros=0
    # Wait for CSV to appear — first reading may take a few seconds.
    until [ -f "${LOG_DIR}/alumet_readings.csv" ]; do sleep 2; done
    while kill -0 "${TEED_PID}" 2>/dev/null; do
        sleep 10
        # Filter last 20 CSV rows for a power metric; print "zero", "ok", or "skip".
        val=$(tail -20 "${LOG_DIR}/alumet_readings.csv" 2>/dev/null \
              | awk -F';' '
                  tolower($2) ~ /power/ {
                      v = $NF; gsub(/ /, "", v); found = 1; last = v + 0
                  }
                  END {
                      if (!found) print "skip"
                      else if (last == 0) print "zero"
                      else print "ok"
                  }')
        case "${val}" in
            zero) zeros=$((zeros + 1)) ;;
            ok)   zeros=0 ;;
            skip) ;;
        esac
        if [ "${zeros}" -ge "${ZERO_THRESHOLD}" ]; then
            echo "[ALUMET-RELAY][WATCHDOG] ${zeros} consecutive zero power readings — restarting container" >&2
            kill "${TEED_PID}" 2>/dev/null
        fi

        # CSV size cap (archive + agent restart): the alumet csv plugin truncates
        # output_path on open (verified: file resets to ~0 on every agent start),
        # so it is NOT append-mode — an in-place truncate would leave a sparse
        # file. Instead, when the live CSV passes CSV_MAX_BYTES, snapshot it to a
        # timestamped archive and kill the agent: Podman restarts the container
        # and the plugin reopens output_path fresh (truncated to 0). force_flush
        # stays on; at most ~1 reading in the cp→restart window is lost (rotation
        # cadence scales with poll rate: ≈2 days at 200 MB / 1 Hz, ≈10 h at 5 Hz).
        csv="${LOG_DIR}/alumet_readings.csv"
        csv_bytes=$(stat -c%s "${csv}" 2>/dev/null || echo 0)
        if [ "${csv_bytes}" -ge "${CSV_MAX_BYTES}" ]; then
            ts=$(date '+%Y%m%d_%H%M%S')
            cp "${csv}" "${LOG_DIR}/alumet_readings_${ts}.csv"
            echo "[ALUMET-RELAY][ROTATE] CSV ${csv_bytes}B ≥ ${CSV_MAX_BYTES}B — archived to alumet_readings_${ts}.csv, restarting agent" >&2
            # Retain newest CSV_KEEP archives; the glob excludes the live file.
            find "${LOG_DIR}" -maxdepth 1 -name 'alumet_readings_*.csv' -type f -printf '%T@ %p\n' \
                | sort -rn | tail -n +$((CSV_KEEP + 1)) | cut -d' ' -f2- | xargs -r rm -f
            kill "${TEED_PID}" 2>/dev/null  # → container restart → plugin truncates live CSV
        fi
    done
) &

wait "${TEED_PID}"
