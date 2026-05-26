"""
PLUDOS FL Round Trigger — autonomous orchestrator for `flwr run .`.

Watches InfluxDB for two signals of gateway readiness and, when enough gateways
are ready, launches an FL round inside the bind-mounted PLUDOS repo at
/app/project. After each run, writes a structured summary to last_run.json so
the operator can verify what happened without trawling container logs.

Readiness signals (either is sufficient, deduplicated by gateway_id):
  • `gw_status` measurement — emitted by client/client.py at startup and after
    each successful evaluate(). Indicates an active gateway with parquet buffer.
  • `stm_mission` measurement — emitted by client/data-engine.py on every
    mission-end flush. Indicates fresh data has arrived since the last FL run.

Restart safety: a pidfile guards against a double-launch when the container
restarts mid-round. Stale pidfiles (process gone) are reclaimed automatically.

All paths and tunables come from env so the container is fully configurable
without rebuilding the image.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from influxdb_client import InfluxDBClient  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TRIGGER] %(levelname)s %(message)s",
)
logger = logging.getLogger("fl-trigger")

# ---------------------------------------------------------------------------
# Configuration — all from env so the container needs no rebuild for tuning.
# ---------------------------------------------------------------------------
INTERVAL_S       = int(os.getenv("FL_TRIGGER_INTERVAL_S", "30"))
MIN_FIT_CLIENTS  = int(os.getenv("FL_MIN_FIT_CLIENTS",    "1"))

# /app/project is the bind-mount target in compose.yaml (PLUDOS repo, read-only).
PROJECT_DIR      = Path(os.getenv("FL_PROJECT_DIR",   "/app/project"))
STATE_DIR        = Path(os.getenv("FL_STATE_DIR",     "/app/state"))
PIDFILE          = Path(os.getenv("FL_TRIGGER_PIDFILE", str(STATE_DIR / "trigger.pid")))
LAST_RUN_FILE    = Path(os.getenv("FL_TRIGGER_LAST_RUN", str(STATE_DIR / "last_run.json")))
HEARTBEAT_FILE   = Path(os.getenv("FL_TRIGGER_HEARTBEAT", str(STATE_DIR / "heartbeat")))
LOGS_DIR         = STATE_DIR / "logs"

INFLUXDB_URL     = os.getenv("INFLUXDB_URL",    "http://influxdb:8086")
INFLUXDB_TOKEN   = os.getenv("INFLUXDB_TOKEN",  "")
INFLUXDB_ORG     = os.getenv("INFLUXDB_ORG",    "pludos")
INFLUXDB_BUCKET  = os.getenv("INFLUXDB_BUCKET", "alumet_energy")

# Regex used to extract round number and per-client accuracy from `flwr run .` stdout.
_ROUND_RE    = re.compile(r"--- ROUND (\d+):")
_ACCURACY_RE = re.compile(r"\[EVAL\] round=(\S+)\s+test_samples=\d+\s+accuracy=([\d.]+)")


# ---------------------------------------------------------------------------
# Pidfile helpers — restart-safe single-launch enforcement.
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Return True if the given pid is still running (POSIX signal 0 probe)."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_pidfile() -> int | None:
    # Returns the live pid, or None if no pidfile / stale pidfile.
    if not PIDFILE.exists():
        return None
    try:
        pid = int(PIDFILE.read_text().strip())
    except (ValueError, OSError):
        return None
    if _pid_alive(pid):
        return pid
    logger.warning("[PID] stale pidfile (pid=%d not alive) — reclaiming", pid)
    try:
        PIDFILE.unlink()
    except OSError:
        pass
    return None


def _write_pidfile(pid: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(pid))


def _clear_pidfile() -> None:
    try:
        PIDFILE.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# InfluxDB readiness queries — union of gw_status and stm_mission signals.
# ---------------------------------------------------------------------------

def _last_run_ts() -> float:
    # Epoch seconds of the last *successful* FL run only. Failed runs return 0
    # so the readiness window resets and the trigger retries on the next tick.
    if not LAST_RUN_FILE.exists():
        return 0.0
    try:
        data = json.loads(LAST_RUN_FILE.read_text())
        if data.get("exit_code", 1) != 0:
            return 0.0
        return float(data.get("finished_at_epoch", 0.0))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("[STATE] last_run.json unreadable (%s) — treating as never-run", exc)
        return 0.0


def _ready_gateways() -> set[str]:
    """
    Query InfluxDB for gateways that have signalled readiness since the last
    successful FL run. Returns the union of:
      • gw_status points in the last 2 × interval with parquet_files_available > 0
      • stm_mission points newer than last_run_ts (any value)

    Uses gateway_id (gw_status) and gateway (stm_mission) tags interchangeably.
    """
    last_ts = _last_run_ts()
    # Use a window at least 2 × poll interval, with a sane floor of 5 minutes
    # so a slow-tick gateway is not missed between polls.
    hb_window_s = max(2 * INTERVAL_S, 300)

    gateways: set[str] = set()
    try:
        client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        try:
            qapi = client.query_api()

            # Signal A — recent heartbeat with non-empty buffer.
            q_status = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -{hb_window_s}s)
  |> filter(fn: (r) => r["_measurement"] == "gw_status")
  |> filter(fn: (r) => r["_field"] == "parquet_files_available")
  |> filter(fn: (r) => r["_value"] > 0)
  |> keep(columns: ["gateway_id"])
  |> distinct(column: "gateway_id")
'''
            for table in qapi.query(q_status):
                for record in table.records:
                    gw = record.values.get("gateway_id")
                    if gw:
                        gateways.add(str(gw))

            # Signal B — mission flushed since the last successful FL run.
            # last_ts is epoch seconds; Flux requires RFC3339 for absolute starts.
            if last_ts > 0:
                start_rfc = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()
            else:
                # No previous run — accept any mission in the rolling window.
                start_rfc = f"-{hb_window_s}s"
            q_mission = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: {start_rfc})
  |> filter(fn: (r) => r["_measurement"] == "stm_mission")
  |> keep(columns: ["gateway"])
  |> distinct(column: "gateway")
'''
            for table in qapi.query(q_mission):
                for record in table.records:
                    gw = record.values.get("gateway")
                    if gw:
                        gateways.add(str(gw))
        finally:
            client.close()
    except Exception as exc:
        logger.warning("[INFLUX] readiness query failed: %s", exc)

    return gateways


# ---------------------------------------------------------------------------
# `flwr run .` launcher — synchronous subprocess with stdout/stderr captured.
# ---------------------------------------------------------------------------

def _run_flwr(ready_gateways: set[str]) -> dict:
    """
    Launch `flwr run .` in the bind-mounted project, wait for completion,
    parse the log for round + per-client accuracy, and return the summary
    dict ready to be JSON-dumped into last_run.json.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    log_path = LOGS_DIR / f"round_{int(started_at)}.log"

    logger.info("[FLWR] launching `flwr run .` (ready=%s, log=%s)",
                sorted(ready_gateways), log_path.name)

    # Open the log file for write — captures stdout and stderr together so the
    # round_total markers and [EVAL] lines interleave correctly.
    with log_path.open("w") as log_fh:
        proc = subprocess.Popen(
            ["flwr", "run", "."],
            cwd=str(PROJECT_DIR),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        _write_pidfile(proc.pid)
        try:
            exit_code = proc.wait()
        except KeyboardInterrupt:
            # Container received SIGTERM — propagate to the child for clean
            # shutdown, then re-raise so the outer loop exits.
            proc.send_signal(signal.SIGTERM)
            proc.wait()
            raise

    finished_at = time.time()
    logger.info("[FLWR] finished exit=%d duration=%.1fs", exit_code, finished_at - started_at)

    # Parse the captured log for round and accuracy info — best-effort.
    max_round = 0
    accuracy_per_client: dict[str, float] = {}
    try:
        for line in log_path.read_text().splitlines():
            m = _ROUND_RE.search(line)
            if m:
                max_round = max(max_round, int(m.group(1)))
                continue
            m = _ACCURACY_RE.search(line)
            if m:
                accuracy_per_client[m.group(1)] = float(m.group(2))
    except OSError as exc:
        logger.warning("[FLWR] log parse failed: %s", exc)

    return {
        "round": max_round,
        "started_at_epoch":  started_at,
        "finished_at_epoch": finished_at,
        "started_at":  datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
        "finished_at": datetime.fromtimestamp(finished_at, tz=timezone.utc).isoformat(),
        "exit_code": exit_code,
        "clients": sorted(ready_gateways),
        "accuracy_per_client": accuracy_per_client,
        "log_file": str(log_path),
    }


def _write_last_run(summary: dict) -> None:
    # Atomic-rename pattern so a partially-written file is never visible.
    tmp = LAST_RUN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, indent=2))
    os.replace(tmp, LAST_RUN_FILE)


# ---------------------------------------------------------------------------
# Main loop — touch heartbeat, evaluate readiness, fire, sleep.
# ---------------------------------------------------------------------------

def _touch_heartbeat() -> None:
    # Compose healthcheck uses `find -mmin` on this file to confirm liveness.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.touch()


def _install_signal_handlers() -> None:
    # Make `podman stop` clean: exit the main loop without leaving a pidfile.
    def _term(*_args):
        logger.info("[SIGNAL] termination requested — exiting after current tick")
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT,  _term)


def main() -> int:
    _install_signal_handlers()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(
        "trigger started (interval=%ds min_clients=%d project=%s)",
        INTERVAL_S, MIN_FIT_CLIENTS, PROJECT_DIR,
    )

    try:
        while True:
            _touch_heartbeat()

            running_pid = _read_pidfile()
            if running_pid is not None:
                logger.info("[PID] FL round already running (pid=%d) — skipping tick", running_pid)
                time.sleep(INTERVAL_S)
                continue

            ready = _ready_gateways()
            if len(ready) < MIN_FIT_CLIENTS:
                logger.info(
                    "[READY] %d/%d gateways ready (%s) — waiting",
                    len(ready), MIN_FIT_CLIENTS, sorted(ready) or "none",
                )
                time.sleep(INTERVAL_S)
                continue

            try:
                summary = _run_flwr(ready)
                _write_last_run(summary)
                logger.info("[STATE] last_run.json updated: round=%d exit=%d",
                            summary["round"], summary["exit_code"])
            finally:
                _clear_pidfile()

            # Sleep one full interval after a round so InfluxDB has time to
            # reflect the post-round gw_status heartbeats before re-evaluating.
            time.sleep(INTERVAL_S)
    except KeyboardInterrupt:
        logger.info("trigger exiting cleanly")
        _clear_pidfile()
        return 0


if __name__ == "__main__":
    sys.exit(main())
