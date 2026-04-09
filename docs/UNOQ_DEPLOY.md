# Deploying incubator-v3 to Arduino UNO Q (Debian)

This guide documents the **local-run first** workflow for UNO Q.

## Normal workflow

```bash
./init_unoq.sh
./scripts/start.sh
# later, after new commits:
./scripts/update.sh
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
python -m pip install --upgrade pip
python -m pip install -e .
```

If `python3 -m uvicorn ...` returns `No module named uvicorn`, run it from the activated venv instead:

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### `./init_unoq.sh`
- Installs minimal OS packages when `apt-get` is available (`python3`, `python3-pip`, `python3-venv`, `git`, `curl`).
- Stops old `incubator-v3*` services and stale uvicorn processes, then refreshes `.venv`.
- Runs import verification with `python -c "import app.main; ..."`.
- Installs/overwrites `/etc/systemd/system/incubator-v3.service`, enables it, and restarts it.
- Leaves the app configured to auto-start at boot.

### `./scripts/start.sh`
- Changes to project root.
- Activates `.venv`.
- Starts the app with:
  - `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`

### `./scripts/update.sh`
- Changes to project root.
- Runs `git pull`.
- Activates `.venv`.
- Reinstalls project dependencies with `python -m pip install -e .`.
- Reloads and restarts `incubator-v3.service` when installed.

## Validate

```bash
./init_unoq.sh
./scripts/start.sh
# later, after new commits:
./scripts/update.sh
```

## What each command does

### `./init_unoq.sh`
- Installs minimal OS packages when `apt-get` is available (`python3`, `python3-pip`, `python3-venv`, `git`, `curl`).
- Creates `.venv` if missing.
- Upgrades pip and installs project dependencies with `python -m pip install -e .`.
- Marks runtime scripts executable.
- Prints next-step commands.

### `./scripts/start.sh`
- Changes to project root.
- Activates `.venv`.
- Starts the app with:
  - `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`

### `./scripts/update.sh`
- Changes to project root.
- Runs `git pull`.
- Activates `.venv`.
- Reinstalls project dependencies with `python -m pip install -e .`.

## Validate

```bash
curl http://127.0.0.1:8000/
curl http://127.0.0.1:8000/docs
curl http://127.0.0.1:8000/health
```

## Service templates

- `deploy/incubator-v3.service`
- `deploy/incubator-v3.env.example`
