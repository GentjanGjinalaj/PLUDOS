# OVERVIEW — server/alumet (server-side energy profiler)

> Newcomer's map of the server energy container. Background: ADR-011 in
> `docs/decisions.md`. The `pludos-alumet` skill applies when editing here.

## Why this folder exists

The thesis measures the energy cost of federated learning across the *whole*
system. This folder measures the **central server's own power** and also acts
as the **collection point** for the Jetsons' energy streams. It is the `alumet`
container in `server/compose.yaml`, and it is the server-side counterpart of
`client/alumet-relay/`.

It runs the same **Alumet** framework, built from the same pinned source
(`v0.9.4`), but with a server-appropriate set of plugins.

## The files

This folder is a container definition — two files, no application code.

| File | Responsibility | Weight |
|------|----------------|--------|
| `Containerfile` | Two-stage build identical in shape to the Jetson sidecar's: compile `alumet-agent` from Rust source, then copy the binary into a minimal Debian runtime. Targets x86_64 (laptop / future server). First build ~20 min, then cached. | Helper (build recipe) |
| `entrypoint.sh` | Writes the Alumet TOML config and launches the agent with four plugins enabled at once. | Core of this container |

## The four plugins (from `entrypoint.sh`)

| Plugin | Role |
|--------|------|
| `rapl` | Reads the server CPU's energy via `/sys/class/powercap` (Intel/AMD RAPL). Falls back to process-level CPU stats if RAPL is unavailable (VM/CI). **ADR-011 Phase 1.** |
| `relay-server` | gRPC receiver on port `50051` for the Jetson `alumet-relay` sidecars to forward their INA3221 streams (**ADR-011 Phase 2**). Listens passively — no connected clients just means silence, not an error. |
| `influxdb` | Persists all readings to the server InfluxDB (`fl_energy` etc.), tagged `ALUMET_DEVICE_TAG=server`. |
| `prometheus-exporter` | Live scrape endpoint on `:9094` for Grafana. |

## Weight and relationships

**Helper, not on the training path** — FL still runs if this is down; you just
lose the server-side energy numbers and the relay aggregation point.

```
server CPU (RAPL) ──┐
Jetson relays ─gRPC─┼─► alumet-agent (this container) ──► InfluxDB ──► Grafana
                    └─► :9094 Prometheus (live scrape) ──┘
```

Two sibling folders complete the energy picture: `client/alumet-relay/`
(measures each Jetson) and `server/grafana/` (draws the panels). All three
write to / read from the InfluxDB defined in `server/compose.yaml`.
