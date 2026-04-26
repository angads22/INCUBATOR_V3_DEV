-- Incubator v3 - Pi Zero 2W Edition
-- Reference schema (SQLAlchemy auto-creates tables on startup)
-- Use this file for migrations, backups, and schema documentation.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    email       TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'owner',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_config (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL UNIQUE,
    claimed     INTEGER NOT NULL DEFAULT 0,
    claim_code  TEXT,
    device_name TEXT,
    farm_name   TEXT,
    wifi_ssid   TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS incubators (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    status  TEXT NOT NULL DEFAULT 'idle'
);

CREATE TABLE IF NOT EXISTS eggs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incubator_id INTEGER NOT NULL REFERENCES incubators(id),
    label        TEXT,
    state        TEXT NOT NULL DEFAULT 'unknown',
    set_date     TEXT,
    candle_day   INTEGER,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS sensor_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incubator_id INTEGER NOT NULL REFERENCES incubators(id),
    temperature_c REAL NOT NULL,
    humidity_pct  REAL NOT NULL,
    captured_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS action_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Vision model inference results linked to an egg image
CREATE TABLE IF NOT EXISTS model_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    egg_id          INTEGER REFERENCES eggs(id),
    image_path      TEXT,
    model_backend   TEXT NOT NULL DEFAULT 'unknown',  -- 'tflite', 'api', 'mock'
    predicted_label TEXT NOT NULL,
    confidence      REAL NOT NULL,
    raw_output      TEXT,  -- JSON blob of full model output
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_settings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL UNIQUE,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_sensor_logs_incubator_time
    ON sensor_logs (incubator_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_model_results_egg
    ON model_results (egg_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_action_logs_time
    ON action_logs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_token
    ON sessions (token_hash);
