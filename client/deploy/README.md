# client/deploy — Jetson boot service

`pludos.service` is the systemd **user-scope** unit that auto-starts the whole
gateway stack on the Jetson after a reboot or a power cut.

## Why this exists

The Jetson runs rootless Podman. Every service in `compose.yaml` already has
`restart: unless-stopped`, which handles *per-container crash recovery* — but
only while Podman is already running. Nothing brings the stack **up at boot**
unless a user service does it, and rootless user services only start at boot
when linger is enabled.

A previous unit ran `podman-compose up -d` **without a profile**. That starts
only the profile-less services (`data-engine`, `alumet-relay`); the standalone
services (`influxdb-local`, `grafana-local`, `ai-worker`) stayed down, so after
a power cut InfluxDB and Grafana never came back and the dashboards went dark.
This unit uses `--profile standalone`, matching `PLUDOS_MODE=standalone` in
`.env`, so the full stack recovers.

## Install

```bash
mkdir -p ~/.config/systemd/user
cp ~/PLUDOS/client/deploy/pludos.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pludos.service
loginctl enable-linger $(whoami)
```

If your Jetson user is not `warehouse1`, edit the `WorkingDirectory` and the two
`podman-compose` absolute paths in `pludos.service` first.

## Verify

```bash
systemctl --user is-enabled pludos.service        # -> enabled
podman ps --format '{{.Names}}'                    # 5 containers:
#   data-engine, alumet-relay, influxdb-local, grafana-local, ai-worker
```

To prove boot recovery without a real power cut:

```bash
systemctl --user stop pludos.service && podman ps   # stack down
systemctl --user start pludos.service && podman ps  # stack back up
```
