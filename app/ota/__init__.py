"""On-device OTA updater (Phase 3).

Pulls app updates from GitHub Releases of this repo, applies them, and — if the
unit isn't healthy afterwards — rolls back automatically. Only the web/UI
service is restarted; the always-on control daemon keeps incubation running
through the update.
"""
