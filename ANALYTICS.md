# PLUDOS Central Analytics Island 📊

This document outlines the setup and configuration of the central monitoring stack used to profile the energy consumption of the Federated Learning architecture via the Alumet API.

The stack utilizes **InfluxDB** as the high-speed time-series database to catch physical telemetry, and **Grafana** for real-time visualization.

## 1. Booting the Analytics Containers

The analytics stack is containerized using Podman. To spin up the database and dashboard in the background, run:

```bash
sudo podman compose -f central-compose.yaml up -d

Service,Access URL,Default Credentials
Grafana: Accessible at http://100.93.249.37:3000 (Default login: admin / admin)
InfluxDB: Accessible at http://100.93.249.37:8086 (Default login: admin / adminpassword)
```
## 2. Connecting Grafana to InfluxDB
Once Grafana is running, you must link it to the InfluxDB container:

1. Navigate to Connections > Data Sources > Add data source.

2. Select InfluxDB.

3. Configure the following exact settings:

   * Query Language: Flux

    * URL: http://influxdb:8086 (Note: Grafana uses the internal Podman network name influxdb, not your external IP, to talk to the database).

    * Organization: pludos

    * Token: pludos-secret-token

    * Default Bucket: alumet_energy

4. Click Save & Test. It should return a "Success" message.

## 3. The Energy Visualization Query (Flux)
Because Alumet samples data at very high frequencies, we use the Flux query language to filter and visualize the specific AI training spikes.

To create the energy consumption graph:

1. Go to a new Dashboard and add a Time series visualization.

2. Switch the query editor from the visual builder to the Raw Query / Script Editor.

3. Paste the following Flux code:
    ```bash
    from(bucket: "alumet_energy")
        |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
        |> filter(fn: (r) => r["_measurement"] == "cpu_energy" or r["_measurement"] == "gpu_energy")
        |> filter(fn: (r) => r["_field"] == "power_w")
        |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
        |> yield(name: "mean")
    ```
### Important Visualization Tips:
* **The Time Trap**: AI training rounds are extremely fast (often < 2 seconds). Ensure your Grafana time range in the top right corner is set to "Last 5 minutes" or you will not see the data spikes.
* **Metrics**: When testing locally on a laptop (TEST_MODE=1), the system simulates cpu_energy. In production on the Jetson Orin Nano, the system logs gpu_energy. The query above safely catches both.