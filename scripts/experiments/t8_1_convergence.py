"""
T8.1 — FL Convergence Study
Queries fl_train_metrics from InfluxDB and plots:
  - training logloss vs FL round (warm-start vs no-warm-start runs)
  - n_estimators adaptation vs round
  - cumulative total_trees vs round

Run after at least one full 10-round FL session:
  python3 scripts/experiments/t8_1_convergence.py [--hours 48] [--out convergence.png]
"""
import argparse
import os
import sys

def _query(influx_url, token, org, bucket, hours):
    from influxdb_client import InfluxDBClient
    client = InfluxDBClient(url=influx_url, token=token, org=org)
    flux = f"""
from(bucket: "{bucket}")
  |> range(start: -{hours}h)
  |> filter(fn: (r) => r._measurement == "fl_train_metrics")
  |> pivot(rowKey: ["_time","gateway_id","fl_round","labeller"],
           columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    tables = client.query_api().query(flux)
    rows = []
    for table in tables:
        for rec in table.records:
            rows.append({
                "time":         rec.get_time(),
                "gateway_id":   rec.values.get("gateway_id", ""),
                "fl_round":     rec.values.get("fl_round", ""),
                "labeller":     rec.values.get("labeller", ""),
                "n_estimators": rec.values.get("n_estimators"),
                "total_trees":  rec.values.get("total_trees"),
                "train_logloss":rec.values.get("train_logloss"),
                "n_samples":    rec.values.get("n_samples"),
                "anomaly_rate": rec.values.get("anomaly_rate"),
            })
    client.close()
    return rows


def _plot(rows, out_path):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: pip install matplotlib")
        sys.exit(1)

    from collections import defaultdict

    # Group by gateway_id to allow multi-Jetson comparison
    by_gateway = defaultdict(list)
    for r in rows:
        by_gateway[r["gateway_id"]].append(r)

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle("T8.1 — FL Convergence Study", fontsize=14)

    for gw_id, gw_rows in by_gateway.items():
        gw_rows.sort(key=lambda r: r["time"])
        rounds     = [r["fl_round"]     for r in gw_rows]
        logloss    = [r["train_logloss"]for r in gw_rows]
        n_est      = [r["n_estimators"] for r in gw_rows]
        total_t    = [r["total_trees"]  for r in gw_rows]
        label      = gw_id.split("-")[0]  # shorten for legend

        axes[0].plot(rounds, logloss,    marker="o", label=label)
        axes[1].plot(rounds, n_est,      marker="s", label=label)
        axes[2].plot(rounds, total_t,    marker="^", label=label)

    axes[0].set_title("Training Logloss vs Round")
    axes[0].set_xlabel("FL Round")
    axes[0].set_ylabel("XGBoost logloss")

    axes[1].set_title("n_estimators per Round (energy adaptation)")
    axes[1].set_xlabel("FL Round")
    axes[1].set_ylabel("n_estimators")

    axes[2].set_title("Cumulative Trees (warm-start growth)")
    axes[2].set_xlabel("FL Round")
    axes[2].set_ylabel("Total boosted rounds")

    for ax in axes:
        ax.legend()
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    print(f"Saved: {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48, help="look-back window in hours")
    ap.add_argument("--out",   default="t8_1_convergence.png")
    args = ap.parse_args()

    influx_url = os.getenv("INFLUXDB_URL",    "http://localhost:8086")
    token      = os.getenv("INFLUXDB_TOKEN",  "pludos-secret-token")
    org        = os.getenv("INFLUXDB_ORG",    "pludos")
    bucket     = os.getenv("INFLUXDB_BUCKET", "alumet_energy")

    print(f"Querying fl_train_metrics from {influx_url} (last {args.hours}h)...")
    rows = _query(influx_url, token, org, bucket, args.hours)
    if not rows:
        print("No fl_train_metrics data found. Run at least one FL session first.")
        sys.exit(1)
    print(f"Found {len(rows)} round records across {len({r['gateway_id'] for r in rows})} gateways.")
    _plot(rows, args.out)


if __name__ == "__main__":
    main()
