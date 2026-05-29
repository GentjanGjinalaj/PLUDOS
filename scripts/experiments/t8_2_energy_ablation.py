"""
T8.2 — Energy Adaptation Ablation
Queries fl_phases + fl_train_metrics from InfluxDB and shows how
n_estimators converges at different FL_ENERGY_BUDGET_J settings.

Usage:
  1. Run four FL sessions with different FL_ENERGY_BUDGET_J values (50, 100, 200, 0=unlimited)
     by editing server/.env between runs.
  2. After all runs, export their time windows via --sessions.

  python3 scripts/experiments/t8_2_energy_ablation.py \\
      --sessions "2026-05-28T10:00:00Z,2026-05-28T11:00:00Z,50" \\
                 "2026-05-28T12:00:00Z,2026-05-28T13:00:00Z,100" \\
                 ...
  OR: query the last 7 days and colour by n_estimators directly (simpler, one session):
  python3 scripts/experiments/t8_2_energy_ablation.py --auto [--hours 168]
"""
import argparse
import os
import sys


def _query_auto(influx_url, token, org, bucket, hours):
    """Query fl_train_metrics and fl_phases to correlate n_estimators with energy_j."""
    from influxdb_client import InfluxDBClient
    client = InfluxDBClient(url=influx_url, token=token, org=org)

    # Get per-round energy from fl_phases (phase=round_total)
    flux_energy = f"""
from(bucket: "{bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "fl_phases" and r.phase == "round_total"
            and r._field == "energy_j")
  |> pivot(rowKey: ["_time","device","fl_round"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    # Get n_estimators from fl_train_metrics
    flux_train = f"""
from(bucket: "{bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "fl_train_metrics")
  |> pivot(rowKey: ["_time","gateway_id","fl_round"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    energy_rows, train_rows = [], []
    for table in client.query_api().query(flux_energy):
        for rec in table.records:
            energy_rows.append({
                "fl_round": rec.values.get("fl_round"),
                "energy_j": rec.values.get("energy_j"),
                "device":   rec.values.get("device"),
            })
    for table in client.query_api().query(flux_train):
        for rec in table.records:
            train_rows.append({
                "fl_round":     rec.values.get("fl_round"),
                "n_estimators": rec.values.get("n_estimators"),
                "gateway_id":   rec.values.get("gateway_id"),
            })
    client.close()
    return energy_rows, train_rows


def _plot(energy_rows, train_rows, out_path):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: pip install matplotlib")
        sys.exit(1)

    # Join on fl_round (best-effort — assumes single gateway)
    energy_by_round = {r["fl_round"]: r["energy_j"] for r in energy_rows}
    paired = [(r["fl_round"], energy_by_round.get(r["fl_round"], float("nan")), r["n_estimators"])
              for r in train_rows]
    paired.sort(key=lambda x: x[0])

    rounds   = [p[0] for p in paired]
    energies = [p[1] for p in paired]
    n_ests   = [p[2] for p in paired]

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 12), sharex=True)
    fig.suptitle("T8.2 — Energy Adaptation Ablation", fontsize=14)

    ax1.bar(rounds, energies, color="steelblue", alpha=0.7)
    ax1.set_ylabel("Energy consumed (J)")
    ax1.set_title("Round energy")
    ax1.grid(True, alpha=0.3, axis="y")

    ax2.step(rounds, n_ests, where="post", color="darkorange", linewidth=2)
    ax2.scatter(rounds, n_ests, color="darkorange")
    ax2.set_ylabel("n_estimators")
    ax2.set_title("Server-assigned n_estimators (energy-adapted)")
    ax2.grid(True, alpha=0.3)

    # Scatter: energy vs n_estimators (the key thesis plot)
    ax3.scatter(energies, n_ests, c=range(len(paired)), cmap="viridis", s=80)
    ax3.set_xlabel("Energy (J)")
    ax3.set_ylabel("n_estimators")
    ax3.set_title("n_estimators vs energy consumed (colour = time)")
    ax3.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto",  action="store_true", help="auto-query last N hours")
    ap.add_argument("--hours", type=int, default=168)
    ap.add_argument("--out",   default="t8_2_energy_ablation.png")
    args = ap.parse_args()

    influx_url = os.getenv("INFLUXDB_URL",    "http://localhost:8086")
    token      = os.getenv("INFLUXDB_TOKEN",  "pludos-secret-token")
    org        = os.getenv("INFLUXDB_ORG",    "pludos")
    bucket     = os.getenv("INFLUXDB_BUCKET", "alumet_energy")

    print(f"Querying energy + train metrics from {influx_url} (last {args.hours}h)...")
    energy_rows, train_rows = _query_auto(influx_url, token, org, bucket, args.hours)
    if not train_rows:
        print("No fl_train_metrics found. Run FL sessions first.")
        sys.exit(1)
    print(f"Found {len(energy_rows)} energy rows, {len(train_rows)} train-metric rows.")
    _plot(energy_rows, train_rows, args.out)


if __name__ == "__main__":
    main()
