---
name: pludos-podman-jetson
description: Reviews and writes Podman compose files, Containerfiles, and Jetson deployment workflows for PLUDOS. Use whenever the user works on client/compose.yaml, server/compose.yaml, client/Containerfile, or asks about deploying to the physical Jetson Orin Nano, configuring systemd user services, joining Tailscale, or debugging container networking. Also use when the user shares container logs, podman commands, or asks about NVIDIA GPU passthrough, tmpfs volumes, or rootless permissions on the Jetson.
---

# PLUDOS Podman / Jetson Deployment Skill

This skill keeps PLUDOS deployments aligned with the project's Podman
conventions and the Jetson Orin Nano's specific quirks. The deployment
target is one Jetson per warehouse, designed to scale to ≥100 shuttles.

## Hardware target

- Jetson Orin Nano Super Developer Kit, 8 GB module
- Ampere GPU 1024 CUDA cores + 32 Tensor cores (67 TOPS)
- 6-core ARM Cortex-A78AE, 8 GB LPDDR5
- 7–25 W operating envelope
- JetPack r35.x, Ubuntu 22.04
- INA3221 power monitor accessible via tegrastats

## Stack rules

1. **Podman, not Docker.** Rootless preferred. `podman-compose` for
   multi-service. The user has explicitly chosen this; don't suggest
   "just use Docker" without a hard reason.
2. **One service per container.** No supervisord-style multi-process
   images.
3. **Pin base images.** The current pin is
   `nvcr.io/nvidia/l4t-pytorch:r35.2.1-pth2.0-py3`. Don't bump without
   discussing — the L4T BSP version must match the Jetson's JetPack.
4. **Named volumes.** Avoid host bind-mounts except for `/dev/net/tun`
   (Tailscale).
5. **Profiles for optional services.** `tailscale` and `ai-worker` are
   behind `profiles: [vpn]` so a fresh clone runs `data-engine` alone
   without joining a tailnet.
6. **Credentials in `.env`, gitignored.** Both `client/.env` and
   `server/.env` are gitignored. Commit `.env.example` files.
7. **GPU passthrough** for `ai-worker` only. The
   `deploy.resources.reservations.devices` block needs:
   ```yaml
   - driver: nvidia
     count: 1
     capabilities: [gpu]
   ```
   Don't propagate this to `data-engine` — it doesn't need the GPU.

## Common tasks

### Reviewing a `compose.yaml` change

Walk through:
1. Image pinned? (no `:latest`)
2. Volumes named, not bind-mounted (except known exceptions)?
3. Profile gating correct? (`vpn`-needing services behind `profiles`)
4. Env vars correct? (no plaintext secrets, defaults are dev-safe)
5. `restart: unless-stopped` (or `always`) on long-running services?
6. Port exposure correct? Data-engine needs UDP 5683 (CoAP) and UDP
   5000 (beacon) bound to host. AI worker needs no host ports.
7. Depends-on order correct? (`ai-worker` after `data-engine` and
   `tailscale`)

### Reviewing a `Containerfile` change

Walk through:
1. Base image pinned?
2. `WORKDIR` set?
3. `requirements.txt` copied before source (so pip layer caches well)?
4. `--no-cache-dir` on pip install (image size)?
5. Necessary system packages installed via `apt-get install -y` with
   `apt-get clean && rm -rf /var/lib/apt/lists/*` to drop the package
   cache?
6. Final `RUN` creates the runtime directories (`/app/ram_buffer`)
   the compose volumes will mount into?

### Working with the remote Jetson via SSH

The user runs PLUDOS on a physical Jetson. Their laptop is the dev
machine; the Jetson is reached over SSH (and over the local Wi-Fi /
Tailscale for the data plane). When you need to operate on the
Jetson, use bash on the user's laptop:

```bash
ssh jetson "<command>"
ssh jetson "cd ~/PLUDOS/client && podman-compose ps"
```

The user is responsible for SSH config (the `Host jetson` block in
`~/.ssh/config`). Don't generate keys, don't propose host-key
changes, don't suggest `ssh-keygen` workflows. Trust their setup.

For long-running commands (image builds 2–10 min, FL training
30s–10 min), don't block on them indefinitely:

- For builds: `ssh jetson "cd ~/PLUDOS/client && podman-compose build data-engine 2>&1 | tee /tmp/build.log"` then poll `/tmp/build.log` if needed.
- For runs: `ssh jetson "podman-compose up -d data-engine"` (detached) then check `podman logs`.
- For interactive debugging: tell the user to open a tmux session on
  the Jetson and paste back relevant excerpts.

### systemd user services on Jetson

For "drop-in deployable" the user wants the data-engine container to
auto-restart and survive reboot. The pattern is `systemctl --user`:

```ini
# ~/.config/systemd/user/pludos-data-engine.service
[Unit]
Description=PLUDOS Data Engine (CoAP server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/<user>/PLUDOS/client
ExecStart=/usr/bin/podman-compose up data-engine
ExecStop=/usr/bin/podman-compose down
Restart=always
RestartSec=10s

[Install]
WantedBy=default.target
```

Then:
```bash
systemctl --user daemon-reload
systemctl --user enable --now pludos-data-engine.service
loginctl enable-linger <user>   # so service starts at boot, no login
```

The `enable-linger` step is non-obvious and easy to miss; mention it.

### Jetson power-mode setting

Out of the box the Jetson Orin Nano boots in MAXN_SUPER (full 25 W).
For energy measurements that match deployment conditions, the user
may want to set a specific power mode:

```bash
sudo nvpmodel -m 0    # MAXN_SUPER, full 25 W
sudo nvpmodel -m 1    # 15 W
sudo nvpmodel -m 2    # 7 W (low-power)
sudo jetson_clocks    # lock clocks to max (deterministic perf)
```

Knowing which mode is active matters for any energy benchmark. If
the user shares Alumet output without saying which mode, ask.

## Anti-patterns

- Don't propose Kubernetes / k3s — overkill for one host per warehouse.
- Don't propose Docker Swarm — same, plus the project is Podman-only.
- Don't propose moving secrets to the YAML — they go in `.env`.
- Don't propose `--privileged` containers — work out the specific
  capabilities/devices needed instead.
- Don't propose host-network mode — use explicit port mappings, even
  for UDP. Host-network is hard to reason about across multiple
  warehouses.

## Diagnosis cheat sheet (for log reviews)

When the user shares output from `podman logs`, common signatures:

- `Error: short-name "..." did not resolve to an alias` — Podman
  rootless registry config issue. Fix by adding the registry to
  `~/.config/containers/registries.conf`.
- `cannot setup namespace using newuidmap` — subuid/subgid not
  configured for the user. `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 <user>` then re-login.
- `permission denied: /var/lib/containers` — ran as root once, then as
  user. Wipe `~/.local/share/containers` and start over rootless.
- `tmpfs: no space left on device` — `shared_ram_buffer` tmpfs filled.
  The data-engine should be flushing to Parquet at 80% buffer; if it
  isn't, the gateway-side buffer policy is broken (see
  `current_problems.md` and `architecture.md`).
- `nvidia-container-cli: requirement error: unsatisfied condition` —
  L4T version of the host doesn't match the base image's L4T tag.
  Either update JetPack or downgrade the base image.
