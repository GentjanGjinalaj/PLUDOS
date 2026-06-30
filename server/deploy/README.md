# server/deploy — central-server boot service

`pludos-server.service` is the systemd **user-scope** unit that auto-starts
the whole central-server stack after a reboot or a power cut.

## Why this exists

The server runs rootless Podman. Every service in `compose.yaml` already has
`restart: unless-stopped`, which handles *per-container crash recovery* — but
only while Podman is already running. Nothing brings the stack **up at boot**
unless a user service does it, and rootless user services only start at boot
when linger is enabled.

This is the server-side twin of `client/deploy/pludos.service` (the Jetson
gateway unit). The difference: the server `compose.yaml` has **no compose
profiles**, so a plain `podman compose up -d` already starts every service
(`influxdb`, `grafana`, `alumet`, `fl-trigger`). The Jetson unit needs
`--profile standalone` because its InfluxDB/Grafana sit behind that profile;
the server unit does not.

This host uses the `podman compose` subcommand (Docker Compose v5.x driver),
not the standalone `podman-compose` binary the Jetson uses.

## Install

```bash
mkdir -p ~/.config/systemd/user
cp ~/PLUDOS/server/deploy/pludos-server.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pludos-server.service
loginctl enable-linger $(whoami)
```

If your server user is not `ggjinalaj`, edit the `WorkingDirectory` in
`pludos-server.service` first.

## Verify

```bash
systemctl --user is-enabled pludos-server.service   # -> enabled
podman ps --format '{{.Names}}'                      # 4 containers:
#   pludos-influxdb, pludos-grafana, pludos-alumet, pludos-fl-trigger
```

To prove boot recovery without a real power cut:

```bash
systemctl --user stop pludos-server.service && podman ps   # stack down
systemctl --user start pludos-server.service && podman ps  # stack back up
```

> Note: `alumet` reads RAPL energy counters and may fail to start on a host
> without the expected `/dev`/sysfs access (it has exited non-zero on the dev
> laptop). That is a separate hardware-access issue; the boot unit still
> brings up InfluxDB, Grafana, and fl-trigger.
