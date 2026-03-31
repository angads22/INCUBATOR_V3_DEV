# Deploying incubator-v3 to Arduino UNO Q (Debian)

This guide documents the **local-run first** workflow for UNO Q.

## Normal workflow

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

## Optional systemd for later

If/when you want background startup via service manager, use these optional templates:

- `deploy/incubator-v3.service`
- `deploy/incubator-v3.env.example`

(systemd is intentionally not forced by `init_unoq.sh` in this workflow)
