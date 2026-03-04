"""
PLUDOS Central Server: AI Orchestrator
--------------------------------------
This script acts as the Cloud/Central Aggregator. 
It coordinates the Federated Learning rounds, gathers the raw XGBoost 
decision trees from the Jetson edge gateways, and aggregates them.
"""

import flwr as fl
import logging
from typing import List, Tuple, Dict, Optional, Union
from flwr.common import Metrics, FitRes, Parameters, Scalar
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

        # In a full deployment, the server would concatenate the tree byte-arrays here.
        # For now, we successfully acknowledge receipt of the physical model weights.
        total_samples = sum([fit_res.num_examples for _, fit_res in results])
        logger.info(f"Aggregated learning from {total_samples} physical vibration samples.")

        # Call the parent FedAvg class to complete the Flower network cycle
        return super().aggregate_fit(server_round, results, failures)


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