"""
PLUDOS Central Server — Flower ServerApp
Coordinates federated learning rounds across Jetson edge gateways.
Aggregation: horizontal tree-set union (ADR-010 Option A).
Energy-aware adaptation: n_estimators adjusted each round based on measured
energy from InfluxDB fl_phases measurement (ADR-014).
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import flwr as fl
import numpy as np
import xgboost as xgb
from flwr.common import (
    FitRes, Parameters, Scalar,
    ndarrays_to_parameters, parameters_to_ndarrays,
)
from flwr.server.client_proxy import ClientProxy
from influxdb_client import InfluxDBClient  # type: ignore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FL topology config — tunable via shell env before `flwr run .`
# ---------------------------------------------------------------------------
NUM_ROUNDS      = int(os.getenv("FL_NUM_ROUNDS",      "3"))
MIN_FIT_CLIENTS = int(os.getenv("FL_MIN_FIT_CLIENTS", "1"))

# ---------------------------------------------------------------------------
# InfluxDB — queried after each round to measure energy and adapt n_estimators.
# These must match the values in server/.env (same machine, InfluxDB runs locally).
# ---------------------------------------------------------------------------
INFLUXDB_URL    = os.getenv("INFLUXDB_URL",    "http://127.0.0.1:8086")
INFLUXDB_TOKEN  = os.getenv("INFLUXDB_TOKEN",  "pludos-secret-token")
INFLUXDB_ORG    = os.getenv("INFLUXDB_ORG",    "pludos")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "alumet_energy")

# ---------------------------------------------------------------------------
# Energy-aware adaptation constants (ADR-014).
# ENERGY_BUDGET_J: max acceptable energy (J) from any single gateway per round.
# Set this after measuring a real baseline on your hardware — 50 J is a placeholder.
# Control law: reduce by 2 if over budget, grow by 1 if under 60% of budget.
# ---------------------------------------------------------------------------
N_ESTIMATORS_DEFAULT = int(os.getenv("FL_N_ESTIMATORS_DEFAULT", "10"))
N_ESTIMATORS_MIN     = int(os.getenv("FL_N_ESTIMATORS_MIN",     "5"))
N_ESTIMATORS_MAX     = int(os.getenv("FL_N_ESTIMATORS_MAX",     "20"))
ENERGY_BUDGET_J      = float(os.getenv("FL_ENERGY_BUDGET_J",    "50.0"))

# Tracks n_estimators across rounds — starts at default, adapted by energy feedback.
_current_n_estimators: int = N_ESTIMATORS_DEFAULT

# ---------------------------------------------------------------------------
# Persisted global models — written after each successful aggregation.
# MODELS_DIR is relative to the working directory of `flwr run .` (repo root).
# Override via FL_MODELS_DIR if the trigger container mounts the repo elsewhere.
# ---------------------------------------------------------------------------
MODELS_DIR = Path(os.getenv("FL_MODELS_DIR", "server/models"))


def _persist_global_model(server_round: int, merged_bytes: bytes) -> None:
    """
    Save the merged booster for this round and atomically refresh the
    `latest.ubj` symlink. Provides a crash-recovery path: a server restarted
    between rounds can resume inference from `MODELS_DIR/latest.ubj` without
    re-running training.

    Best-effort: persistence failures are logged but do not fail the round —
    the next aggregation will write a new copy.
    """
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)

        # Round it to xgboost's native UBJSON format so it can be loaded with
        # xgb.Booster.load_model(path) directly (no manual byte-buffer plumbing).
        booster = xgb.Booster()
        booster.load_model(bytearray(merged_bytes))

        round_path = MODELS_DIR / f"global_model_round_{server_round}.ubj"
        booster.save_model(str(round_path))

        # Atomic symlink update: write to a temp name, then os.replace() it
        # onto latest.ubj. Avoids a window where latest.ubj points nowhere.
        tmp_link = MODELS_DIR / "latest.ubj.tmp"
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        # Relative symlink target — survives moving the models dir.
        tmp_link.symlink_to(round_path.name)
        os.replace(tmp_link, MODELS_DIR / "latest.ubj")

        logger.info(
            "[MODEL] persisted round %d → %s (%d B), latest.ubj refreshed",
            server_round, round_path.name, round_path.stat().st_size,
        )
    except Exception as exc:
        logger.error("[MODEL] persistence failed for round %d: %s", server_round, exc)


# ---------------------------------------------------------------------------
# Energy query — reads InfluxDB for the previous round's measured energy
# ---------------------------------------------------------------------------

def _query_last_round_energy(prev_round: int) -> float | None:
    """
    Query InfluxDB for the maximum per-gateway energy_j from the given FL round.

    Uses fl_phases measurement with phase=round_total. Queries the max across
    all gateways so adaptation is driven by the most energy-constrained device.
    Returns None on any failure so the caller keeps current n_estimators without
    disrupting the round.
    """
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    try:
        # group() merges all device tables into one; max() then finds the peak
        # energy_j across all gateways for this round.
        query = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -2h)
  |> filter(fn: (r) => r["_measurement"] == "fl_phases")
  |> filter(fn: (r) => r["_field"] == "energy_j")
  |> filter(fn: (r) => r["phase"] == "round_total")
  |> filter(fn: (r) => r["fl_round"] == "{prev_round}")
  |> group()
  |> max()
'''
        tables = client.query_api().query(query)
        for table in tables:
            for record in table.records:
                return float(record.get_value())
    except Exception as exc:
        logger.warning("[ENERGY] InfluxDB query for round %d failed: %s", prev_round, exc)
    finally:
        client.close()
    return None


# ---------------------------------------------------------------------------
# XGBoost federated aggregation — tree-set union (ADR-010 Option A)
# ---------------------------------------------------------------------------

def _merge_boosters(raw_streams: list[bytes]) -> bytes:
    """
    Horizontal tree-set union (ADR-010 Option A).

    Parses each client's booster JSON, concatenates all tree objects,
    re-sequences tree IDs (each client produces IDs 0..N-1, which would
    collide after merge), updates num_trees, and returns the merged booster
    as UTF-8 JSON bytes that XGBoost can load directly.

    Tree count after merge: sum of all client tree counts.
    tree_info stays all-zeros — correct for binary classification.
    """
    all_trees: list = []
    base_model: Optional[dict] = None

    for raw in raw_streams:
        model_dict = json.loads(raw.decode("utf-8"))
        trees = model_dict["learner"]["gradient_booster"]["model"]["trees"]
        if base_model is None:
            base_model = model_dict
            all_trees = list(trees)
        else:
            all_trees.extend(trees)

    # Re-number tree IDs so they are globally unique in the merged booster.
    for idx, tree in enumerate(all_trees):
        tree["id"] = idx

    gb_model = base_model["learner"]["gradient_booster"]["model"]
    gb_model["trees"]     = all_trees
    gb_model["tree_info"] = [0] * len(all_trees)
    gb_model["gbtree_model_param"]["num_trees"] = str(len(all_trees))

    return json.dumps(base_model).encode("utf-8")


class XGBoostStrategy(fl.server.strategy.FedAvg):
    """
    Federated XGBoost aggregation via horizontal tree-set union (ADR-010 Option A).
    Single-client rounds return the client's booster unchanged.
    Multi-client rounds merge all trees and validate the result loads correctly.
    """

    def aggregate_fit(
        self,
        server_round: int,
        results: List[Tuple[ClientProxy, FitRes]],
        failures: List[Union[Tuple[ClientProxy, FitRes], BaseException]],
    ) -> Tuple[Optional[Parameters], Dict[str, Scalar]]:

        if not results:
            return None, {}

        logger.info("--- ROUND %d: aggregating from %d gateway(s) ---",
                    server_round, len(results))

        total_samples = sum(fit_res.num_examples for _, fit_res in results)

        # Decode raw booster bytes from each client's NumPy parameter array.
        raw_streams: list[bytes] = []
        for _, fit_res in results:
            if not fit_res.parameters:
                continue
            try:
                arrays = parameters_to_ndarrays(fit_res.parameters)
                if len(arrays) == 1:
                    raw_streams.append(arrays[0].tobytes())
            except Exception as exc:
                logger.warning("Could not decode booster from a client: %s", exc)

        if not raw_streams:
            logger.warning("No valid boosters — falling back to FedAvg default.")
            return super().aggregate_fit(server_round, results, failures)

        if len(raw_streams) == 1:
            # Single gateway: pass its booster through unchanged.
            merged_bytes = raw_streams[0]
            logger.info("Single gateway: booster forwarded unchanged (%d B).", len(merged_bytes))
        else:
            # Multiple gateways: merge all trees, validate, fall back to largest on failure.
            try:
                merged_bytes = _merge_boosters(raw_streams)
                check = xgb.Booster()
                check.load_model(bytearray(merged_bytes))
                n_trees = check.num_boosted_rounds()
                logger.info(
                    "Tree-set union: %d gateways → %d trees total (%d B).",
                    len(raw_streams), n_trees, len(merged_bytes),
                )
            except Exception as exc:
                # Covers both merge errors (bad JSON, missing keys) and load failures.
                logger.error("Booster merge/validation failed (%s). Falling back to largest.", exc)
                merged_bytes = max(raw_streams, key=len)

        # Persist the round's authoritative global model so a crashed server can
        # recover without re-training. Runs after the merge succeeds (or the
        # single-gateway passthrough) and never fails the round.
        _persist_global_model(server_round, merged_bytes)

        parameters = ndarrays_to_parameters(
            [np.frombuffer(merged_bytes, dtype=np.uint8)]
        )
        return parameters, {"total_samples": total_samples}


# ---------------------------------------------------------------------------
# fit_config — energy-aware n_estimators adaptation (ADR-014)
# ---------------------------------------------------------------------------

def fit_config(server_round: int) -> dict:
    """
    Returns training config for each client.

    From round 2 onwards, queries InfluxDB for the previous round's peak
    gateway energy and adapts n_estimators to stay within ENERGY_BUDGET_J.
    Control law: reduce by 2 when over budget (fast response), grow by 1
    when under 60% of budget (slow growth). The asymmetry prevents thrashing.
    """
    global _current_n_estimators

    if server_round > 1:
        energy_j = _query_last_round_energy(server_round - 1)
        if energy_j is not None:
            if energy_j > ENERGY_BUDGET_J:
                _current_n_estimators = max(N_ESTIMATORS_MIN, _current_n_estimators - 2)
                logger.info(
                    "[ENERGY] Round %d: prev=%.2fJ > budget=%.2fJ → n_estimators=%d (reduced)",
                    server_round, energy_j, ENERGY_BUDGET_J, _current_n_estimators,
                )
            elif energy_j < ENERGY_BUDGET_J * 0.6:
                _current_n_estimators = min(N_ESTIMATORS_MAX, _current_n_estimators + 1)
                logger.info(
                    "[ENERGY] Round %d: prev=%.2fJ < 60%% budget → n_estimators=%d (grew)",
                    server_round, energy_j, _current_n_estimators,
                )
            else:
                logger.info(
                    "[ENERGY] Round %d: prev=%.2fJ within budget → n_estimators=%d (stable)",
                    server_round, energy_j, _current_n_estimators,
                )
        else:
            logger.info(
                "[ENERGY] Round %d: no energy data for round %d — keeping n_estimators=%d",
                server_round, server_round - 1, _current_n_estimators,
            )

    return {"server_round": server_round, "n_estimators": _current_n_estimators}


def server_fn(context: fl.common.Context):
    strategy = XGBoostStrategy(
        min_available_clients=MIN_FIT_CLIENTS,
        min_fit_clients=MIN_FIT_CLIENTS,
        min_evaluate_clients=MIN_FIT_CLIENTS,
        on_fit_config_fn=fit_config,
    )
    config = fl.server.ServerConfig(num_rounds=NUM_ROUNDS)
    return fl.server.ServerAppComponents(strategy=strategy, config=config)


app = fl.server.ServerApp(server_fn=server_fn)
