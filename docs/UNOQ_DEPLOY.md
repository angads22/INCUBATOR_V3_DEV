# Deploying incubator-v3 to Arduino UNO Q (Debian)

This guide shows a practical Linux deployment path for UNO Q.

## Fast path (single command after pull)

If your repo is already on the UNO Q and you want a one-shot initialize/update, run:

```bash
sudo ./init_unoq.sh
```

This script installs dependencies, writes runtime env config, installs/updates the systemd unit, restarts service, and checks `/health`.

## 1) Copy code to UNO Q

From your dev machine:

```bash
git clone <your-repo-url>
# or
rsync -avz ./INCUBATOR_V3_DEV/ user@<unoq-ip>:/opt/incubator-v3/
```

On UNO Q, target directory example:

```bash
sudo mkdir -p /opt/incubator-v3
sudo chown -R "$USER":"$USER" /opt/incubator-v3
cd /opt/incubator-v3
```

## 2) Create runtime env

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## 3) Configure serial path and database location

Set environment values that match your UNO Q + ESP32 wiring:

- `INCUBATOR_SERIAL_PORT` (example `/dev/ttyUSB0` or `/dev/ttyACM0`)
- `INCUBATOR_SERIAL_BAUD` (example `115200`)
- `INCUBATOR_DB_URL` (default `sqlite:///./incubator.db`)

Create env file:

```bash
sudo cp deploy/incubator-v3.env.example /etc/incubator-v3.env
sudo nano /etc/incubator-v3.env
```

## 4) Install and start systemd service

```bash
sudo cp deploy/incubator-v3.service /etc/systemd/system/incubator-v3.service
sudo systemctl daemon-reload
sudo systemctl enable incubator-v3.service
sudo systemctl start incubator-v3.service
```

Check status/logs:

```bash
systemctl status incubator-v3.service
journalctl -u incubator-v3.service -f
```

## 5) Validate

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/setup/status
```

## OTA/update pattern for new code

```bash
cd /opt/incubator-v3
git pull
source .venv/bin/activate
pip install -e .
sudo systemctl restart incubator-v3.service
```

## Notes

- Keep ESP32 firmware and UART command protocol versioned in lockstep with this backend.
- For BLE onboarding, ESP32 provisioning implementation will forward payload to UNO Q `/setup/complete` equivalent flow.
