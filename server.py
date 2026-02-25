import flwr as fl
import logging

# Set up logging so we can see what the server is doing
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def fit_config(server_round: int):
    """
    This function sends configuration data to the Jetson clients before each round.
    We pass the round number so the Jetson can tag its Alumet energy metrics with it!
    """
    return {"server_round": server_round}

def main():
    logger.info("Initializing PLUDOS Central Federated Learning Server...")

    # Define the aggregation strategy
    # Note: We configure it to wait for at least 1 client since you will start with 1 Jetson.
    strategy = fl.server.strategy.FedAvg(
        fraction_fit=1.0,           # Train on 100% of available clients
        fraction_evaluate=1.0,      # Evaluate on 100% of available clients
        min_fit_clients=1,          # Minimum number of Jetsons required to start training
        min_evaluate_clients=1,     # Minimum number of Jetsons required to evaluate
        min_available_clients=1,    # Wait until at least 1 Jetson connects before starting
        on_fit_config_fn=fit_config # Send the config to the clients
    )

    # Start the gRPC server listening on port 8080
    logger.info("Starting gRPC listener on [::]:8080")
    fl.server.start_server(
        server_address="0.0.0.0:8080",
        config=fl.server.ServerConfig(num_rounds=3), # We will run 3 global training rounds
        strategy=strategy,
    )

if __name__ == "__main__":
    main()