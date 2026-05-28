#!/usr/bin/env python3
"""
Calibrate FL_ENERGY_BUDGET_J from recent InfluxDB fl_phases data.

Usage:
  python calibrate_energy_budget.py [--rounds N] [--margin M]

Environment (must match server/.env):
  INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET

Algorithm: FL_ENERGY_BUDGET_J = MARGIN × mean(round_total over last N rounds)
           Default: N=5, MARGIN=0.85 (as per to-do spec)
Outputs:   RECOMMENDED FL_ENERGY_BUDGET_J=<value>
"""
import argparse
import os
import sys

from influxdb_client import InfluxDBClient  # type: ignore

INFLUXDB_URL    = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN",  "pludos-secret-token")
INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG",    "pludos")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "alumet_energy")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int,   default=5,    help="Recent rounds to average (default 5)")
    parser.add_argument("--margin", type=float, default=0.85, help="Safety margin on mean energy (default 0.85)")
    args = parser.parse_args()

    query = f"""
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) => r["_measurement"] == "fl_phases")
  |> filter(fn: (r) => r["phase"] == "round_total")
  |> filter(fn: (r) => r["_field"] == "energy_j")
  |> sort(columns: ["_time"], desc: true)
  |> limit(n: {args.rounds})
"""
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    try:
        tables   = client.query_api().query(query=query)
        energies = [rec.get_value() for table in tables for rec in table.records]
    except Exception as exc:
        print(f"ERROR: InfluxDB query failed: {exc}")
        sys.exit(1)
    finally:
        client.close()

    if not energies:
        print("ERROR: no fl_phases/round_total data — run at least one FL round first")
        sys.exit(1)

    mean_j   = sum(energies) / len(energies)
    budget_j = args.margin * mean_j

    print(f"Rounds queried : {len(energies)}")
    print(f"Energies (J)   : {[round(e, 2) for e in energies]}")
    print(f"Mean energy    : {mean_j:.2f} J")
    print(f"Margin         : {args.margin:.2f}")
    print(f"RECOMMENDED FL_ENERGY_BUDGET_J={budget_j:.1f}")


if __name__ == "__main__":
    main()
