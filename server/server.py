"""
PLUDOS Central Server: AI Orchestrator
--------------------------------------
This script acts as the Cloud/Central Aggregator. 
It coordinates the Federated Learning rounds, gathers the raw XGBoost 
decision trees from the Jetson edge gateways, and aggregates them.
"""

import flwr as fl
import logging
import numpy as np
from typing import List, Tuple, Dict, Optional, Union
from flwr.common import Metrics, FitRes, Parameters, Scalar, ndarrays_to_parameters, parameters_to_ndarrays
from flwr.server.client_proxy import ClientProxy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class XGBoostStrategy(fl.server.strategy.FedAvg):
    """
    Custom Federated Strategy for XGBoost. 
    Instead of mathematically averaging weights (like Neural Networks), 
    this strategy collects the tree structures from the edge clients.
    """
    def aggregate_fit(
        self,
        server_round: int,
        results: List[Tuple[ClientProxy, FitRes]],
        failures: List[Union[Tuple[ClientProxy, FitRes], BaseException]],
    ) -> Tuple[Optional[Parameters], Dict[str, Scalar]]:
        
        if not results:
            return None, {}

        logger.info(f"--- SERVER AGGREGATING ROUND {server_round} ---")
        logger.info(f"Received XGBoost trees from {len(results)} Edge Gateway(s).")

        total_samples = sum([fit_res.num_examples for _, fit_res in results])
        logger.info(f"Aggregated learning from {total_samples} physical vibration samples.")

        # Parse each client's raw metrics and extract XGBoost bytes. In client.py,
        # the model is sent as a single NumPy parameter array containing raw libxgboost bytes.
        xgb_raw_streams = []
        for _, fit_res in results:
            if not fit_res.parameters:
                continue
            try:
                client_arrays = parameters_to_ndarrays(fit_res.parameters)
                if len(client_arrays) == 1:
                    xgb_raw_streams.append(client_arrays[0].tobytes())
            except Exception as exc:
                logger.warning(f"Unable to decode parameters from client: {exc}")

        if not xgb_raw_streams:
            logger.warning("No valid XGBoost boosters found from clients. Falling back to default FedAvg behavior.")
            return super().aggregate_fit(server_round, results, failures)

        # Basic merge heuristic (proof-of-concept): choose the largest booster payload
        # for propagation. A full federated XGBoost aggregator should merge tree sets
        # or adopt a model-distillation strategy.
        aggregated_booster = max(xgb_raw_streams, key=len)
        logger.info(f"Aggregated booster size: {len(aggregated_booster)} bytes.")

        parameters = ndarrays_to_parameters([np.frombuffer(aggregated_booster, dtype=np.uint8)])
        return parameters, {"num_round_samples": total_samples}


# This explicitly sends the Round Number to the Jetson so Alumet can tag it!
def fit_config(server_round: int):
    return {"server_round": server_round}


# Define the global server configuration (e.g., 3 rounds of training)
def server_fn(context: fl.common.Context):
    # Use our custom XGBoost Strategy
    strategy = XGBoostStrategy(
        min_available_clients=1,     # Minimum Jetsons required to start
        min_fit_clients=1,
        min_evaluate_clients=1,      # Tells Flower we only have 1 Jetson for evaluation
        on_fit_config_fn=fit_config  # Passes the Round Number to client.py
    )
    config = fl.server.ServerConfig(num_rounds=3)
    return fl.server.ServerAppComponents(strategy=strategy, config=config)

# Initialize the Server App
app = fl.server.ServerApp(server_fn=server_fn)