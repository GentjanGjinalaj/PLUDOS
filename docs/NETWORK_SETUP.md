# Network Setup — WiFi + Tailscale

Two physical networks are involved in a PLUDOS deployment:

1. **Local 2.4 GHz WiFi** — shuttles ↔ Jetson gateway. Telemetry only.
2. **Tailscale tailnet** — Jetson gateway ↔ central server. FL gRPC + InfluxDB
   writes. Crosses NAT, works from any internet uplink.

This doc covers both. For the end-to-end deployment sequence (containers, FL,
calibration), see `docs/DEPLOYMENT_GUIDE.md`.

> **Security note:** real SSIDs, passwords, and tokens are NOT committed.
> Configure them locally in `STM_Shuttles/PLUDOS_Edge_Node/Core/Inc/wifi_credentials.h`
> (gitignored) on the STM32 side, in `client/.env` on the Jetson, and in
> `server/.env` on the server.

---

## 1. Local WiFi (STM32 ↔ Jetson)

The MXCHIP EMW3080 WiFi module **only supports 2.4 GHz**. It will not join a
5 GHz or auto-band network.

| Setting | Requirement |
|---|---|
| Band | **2.4 GHz only** — disable 5 GHz or use a 2.4 GHz-only hotspot |
| Security | WPA2-AES |
| SSID | Your choice — must match `WIFI_SSID` define in firmware |
| Password | Your choice — must match `WIFI_PASSWORD` define in firmware |

For a phone hotspot (Android), enable "Extended compatibility" / "2.4 GHz band".

### Jetson IP discovery

Zero-touch via UDP beacon: the `data-engine` container broadcasts
`PLUDOS-GW:<ip>[:csv-shuttle-ids]` to `255.255.255.255:5000` every 10 s. The
STM32 `BEACON_Run()` discovers the gateway IP at runtime — at boot, on every
WiFi reconnect, and periodically while IDLE — and updates its `JETSON_IP`
runtime variable from the beacon. The `JETSON_IP` define in `wifi_credentials.h`
is the **compile-time fallback** for the very first probe; once a beacon is
seen, the runtime IP takes over.

Manual lookup (if you need to verify):

```bash
# On the Jetson:
ip -4 addr show wlan0
hostname -I
```

### Local ports (open on the Jetson)

| Port | Protocol | Purpose |
|---|---|---|
| 5683 | UDP | 24-byte `PludosTelemetry` stream (ADR-016 v3) |
| 5684 | UDP | High-rate PSRAM capture drain (ADR-020/021) |
| 5000 | UDP | Beacon broadcast (zero-touch IP provisioning) |

```bash
sudo ufw allow 5683/udp
sudo ufw allow 5684/udp
sudo ufw allow 5000/udp
sudo ufw status
```

Under the ADR-021 duty cycle the radio is off during MOVING; high-rate IMU is
captured to PSRAM and drained as a UDP burst on 5684 after the run. The 5683
listener still exists for IDLE telemetry but the firmware no longer transmits a
live stream during MOVING.

---

## 2. Tailscale (Jetson ↔ Server)

Tailscale is the WireGuard-based overlay between Jetson gateways and the central
server (ADR-007). All Flower gRPC traffic and all InfluxDB writes from the
Jetson go through it, so the underlying internet uplink can be anything (home
WiFi, phone hotspot, ethernet) without configuring NAT or VPN gateways.

### 2.1 Install Tailscale on the central server

The Jetson already runs Tailscale inside a sidecar container
(`client/compose.yaml` `tailscale` service, gated by `--profile vpn`). The
server side currently does **not** have a containerised Tailscale — install on
the host.

Ubuntu / Debian:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=pludos-server
# Follow the URL printed; authenticate in your browser.

# Verify:
tailscale ip -4         # → 100.x.x.x  (your server's tailnet address)
tailscale status         # list all peers + last-seen
```

Make the daemon start at boot:

```bash
sudo systemctl enable --now tailscaled
```

> Why not containerised on the server? Tailscale needs the host's `/dev/net/tun`
> and ideally direct kernel access for WireGuard performance. On the Jetson the
> container is acceptable because it joins as a separate node; on the server
> the laptop *is* the FL endpoint, so host install is simpler and faster.

### 2.2 Pick up the server's tailnet IP on each Jetson

From the server, get the IP:

```bash
tailscale ip -4
# e.g. 100.92.45.211
```

On each Jetson, set this in `client/.env`:

```bash
# InfluxDB writes from data-engine and AlumetProfiler
INFLUXDB_URL=http://100.92.45.211:8086

# Flower SuperLink (Mode B only — see DEPLOYMENT_GUIDE.md §3.6)
FLOWER_SUPERLINK_ADDRESS=100.92.45.211:9091
```

> `FLOWER_SUPERLINK_ADDRESS` is consumed by `flower-supernode --superlink
> $FLOWER_SUPERLINK_ADDRESS` (Mode B). Mode A (`flwr run .` simulation) does
> not use it — clients are spawned in-process on the server.

### 2.3 Server-side firewall — open the FL ports to the tailnet only

The Tailscale CGNAT range is `100.64.0.0/10`. Restrict the server's listening
ports to that range so InfluxDB and Flower are reachable only over the
tailnet — never on the public internet.

```bash
sudo ufw allow 8086/tcp from 100.64.0.0/10    # InfluxDB writes from Jetson
sudo ufw allow 9091/tcp from 100.64.0.0/10    # Flower SuperLink gRPC
sudo ufw status
```

### 2.4 Tailscale ACL — restrict 9091 to the gateway tag (optional, recommended)

If your tailnet has multiple devices (e.g. personal laptops), restrict the
Flower port to specifically the Jetsons. In the Tailscale admin console
(https://login.tailscale.com/admin/acls), add tags + an ACL rule:

```jsonc
{
  "tagOwners": {
    "tag:pludos-gateway": ["autogroup:admin"],
    "tag:pludos-server":  ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src":    ["tag:pludos-gateway"],
      "dst":    ["tag:pludos-server:9091", "tag:pludos-server:8086"]
    }
  ]
}
```

Then apply the tags to your nodes:

```bash
# On the server:
sudo tailscale up --advertise-tags=tag:pludos-server

# On each Jetson (inside the container, run via `podman exec`):
podman exec pludos-tailscale tailscale up --authkey=$TS_AUTHKEY --advertise-tags=tag:pludos-gateway
```

**Default tailnet ACL (development):** if you have not edited ACLs in the
admin console, the default policy is `accept all` between tailnet members.
Steps 2.3 (host firewall) are sufficient for dev. Add the ACL above before
inviting other devices to the tailnet.

### 2.5 Verify end-to-end reachability

From each Jetson:

```bash
tailscale ip -4                         # confirm Jetson tailnet IP
ping -c 2 <server-tailscale-ip>         # must succeed
curl -s http://<server-tailscale-ip>:8086/health | python3 -m json.tool
# → { "status": "pass", ... }
```

If the InfluxDB check fails: confirm step 2.3 ran (or `sudo ufw status` shows
ALLOW for 8086/tcp from `100.64.0.0/10`). If the server reboot wiped the rule,
add the rule to `/etc/ufw/before.rules` or use the `ufw-persistent` pattern.

The full pre-flight checklist lives in `docs/DEPLOYMENT_GUIDE.md` Pre-Test
Connection Checklist — that doc is the canonical place to verify every link.

---

## 3. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| STM32 stuck on "Connecting WiFi…" | Hotspot is 5 GHz or hidden | Force 2.4 GHz, broadcast SSID |
| STM32 connects but no `JETSON_IP` set | Beacon didn't reach STM32 (boot probe timed out) | Verify `data-engine` is running and `network_mode: host`; see DEPLOYMENT_GUIDE.md §3.5; bump `JETSON_IP` define as compile-time fallback |
| `curl http://server-tailscale-ip:8086/health` from Jetson hangs | Server firewall blocking | `sudo ufw allow 8086/tcp from 100.64.0.0/10` on server |
| `flower-supernode` can't reach SuperLink | Tailscale ACL or server firewall | Step 2.3 + 2.4 |
| Server tailnet IP changes after reboot | Tailscale daemon not enabled | `sudo systemctl enable --now tailscaled` |
