"""Safety-critical incubation control loop (Phase 3).

Runs as its own always-on process (``python -m app.control``) so that updating
the web/UI app — which restarts only ``incubator.service`` — never pauses
heater or egg-turn control. This is the redundancy that keeps incubation
running *through* an update.
"""
