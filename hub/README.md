# Incubator fleet hub (Phase 2)

A small always-on box on the same LAN (a dedicated Pi 5, or one existing unit
doubling as hub) that gives the fleet:

- **Mosquitto (MQTT broker)** — every unit publishes `fleet/<device_id>/{status,
  temp,humidity}` and subscribes to `fleet/<device_id>/cmd`
  (see `app/services/fleet_service.py`).
- **dnsmasq naming** — friendly `incubator1.<domain>`, `incubator2.<domain>` …
  instead of raw IPs, optionally with DHCP reservations by MAC.

Remote access from outside the LAN is fronted separately by your domain /
reverse-proxy (Phase 4); the hub is the LAN aggregation point.

## Setup

```bash
cp hub/units.example.csv hub/units.csv     # then edit: <mac>,<name>,<ip> per unit
sudo DNS_DOMAIN=incubator.lan ./hub/setup-hub.sh
```

Then point each incubator at the broker (in `/etc/incubator.env`):

```ini
MQTT_ENABLED=true
MQTT_HOST=<hub-ip-or-hub.incubator.lan>
```
…and restart the unit. Confirm a name resolves: `getent hosts incubator1.incubator.lan`.

## DNS-only vs DHCP

- **DNS-only (default, recommended):** the hub publishes `host-record` names; your
  existing router keeps doing DHCP. Give each unit a static/reserved IP that
  matches `units.csv`. Devices must use the hub as a DNS server (set it as the
  DNS server on the router, or per-device).
- **Hub-as-DHCP:** set `DHCP_RANGE="192.168.1.100,192.168.1.150,12h"` so dnsmasq
  also hands out the reserved IPs by MAC (`dhcp-host`). Only do this if you're
  replacing the router's DHCP — don't run two DHCP servers on one LAN.

## Port 53 / systemd-resolved

If `dnsmasq` won't start because port 53 is taken by `systemd-resolved`:

```bash
sudo sed -i 's/^#\?DNSStubListener=.*/DNSStubListener=no/' /etc/systemd/resolved.conf
sudo systemctl restart systemd-resolved
sudo ./hub/setup-hub.sh
```

## Naming

Units are named `incubatorN` (not "Life Loop", which is reserved/conflicted).
Re-run `setup-hub.sh` after editing `units.csv` to apply changes.
