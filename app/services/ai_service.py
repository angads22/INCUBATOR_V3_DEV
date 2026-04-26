"""
AI insights service — interprets live sensor data and vision results.

Currently uses rule-based logic.  The generate_dashboard_insight() method
is designed as a drop-in replacement point for a real LLM call (Claude, GPT,
local Llama) that would receive the same inputs as a structured prompt.

LLM INTEGRATION HOOK
--------------------
Replace the body of generate_dashboard_insight() with an LLM call:

  from anthropic import Anthropic
  client = Anthropic()
  response = client.messages.create(
      model="claude-sonnet-4-6",
      max_tokens=256,
      messages=[{"role": "user", "content": _build_prompt(temperature_c, humidity_pct, ...)}],
  )
  # Parse response into AIInsight fields

Or for fully local inference, call an Ollama / llama.cpp endpoint at
http://localhost:11434/api/generate (fits in ~4 GB RAM, but NOT on Pi Zero 2W's
512 MB — use a remote/cloud endpoint instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


_RISK_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _escalate(current: str, candidate: str) -> str:
    """Return whichever risk level is higher by severity rank."""
    return candidate if _RISK_RANK.get(candidate, 0) > _RISK_RANK.get(current, 0) else current


@dataclass(frozen=True)
class AIInsight:
    summary: str
    confidence: str
    recommendation: str
    risk_level: str       # "low" | "medium" | "high" | "critical"
    generated_at: str


class AIService:
    """Structured analysis of incubator sensor readings and vision results."""

    # Target ranges for chicken eggs (adjust per species)
    TEMP_LOW = 37.2
    TEMP_HIGH = 37.8
    TEMP_CRITICAL_LOW = 36.0
    TEMP_CRITICAL_HIGH = 39.0
    HUMIDITY_LOW = 50.0
    HUMIDITY_HIGH = 65.0
    HUMIDITY_LOCKDOWN_LOW = 65.0   # Last 3 days before hatch

    def generate_dashboard_insight(
        self,
        temperature_c: float,
        humidity_pct: float,
        incubation_day: int = 0,
        vision_label: str | None = None,
    ) -> AIInsight:
        """
        LLM INTEGRATION HOOK — replace this method body to call a real model.

        Inputs available for the prompt:
          - temperature_c, humidity_pct  (current sensor readings)
          - incubation_day               (day 1-21 for chicken eggs)
          - vision_label                 (last candling result, e.g. 'fertile', 'blood_ring')
        """
        issues: list[str] = []
        recommendations: list[str] = []
        risk = "low"

        # Temperature assessment
        if temperature_c >= self.TEMP_CRITICAL_HIGH:
            issues.append(f"CRITICAL: Temperature {temperature_c:.1f}°C is dangerously high")
            recommendations.append("Cut heater power immediately and improve ventilation.")
            risk = "critical"
        elif temperature_c > self.TEMP_HIGH:
            issues.append(f"Temperature {temperature_c:.1f}°C is above target range")
            recommendations.append("Reduce heater duty cycle. Check fan is running.")
            risk = _escalate(risk, "medium")
        elif temperature_c < self.TEMP_CRITICAL_LOW:
            issues.append(f"CRITICAL: Temperature {temperature_c:.1f}°C is too low")
            recommendations.append("Check heater connection. Increase heater duty cycle.")
            risk = "critical"
        elif temperature_c < self.TEMP_LOW:
            issues.append(f"Temperature {temperature_c:.1f}°C is below target range")
            recommendations.append("Increase heater output gradually. Re-check in 10 minutes.")
            risk = _escalate(risk, "medium")

        # Humidity assessment
        lockdown = incubation_day >= 18
        hum_target_low = self.HUMIDITY_LOCKDOWN_LOW if lockdown else self.HUMIDITY_LOW
        if humidity_pct < hum_target_low:
            issues.append(f"Humidity {humidity_pct:.1f}% is below {'lockdown' if lockdown else 'incubation'} target")
            recommendations.append("Add water to humidity reservoir or increase wick surface area.")
            if risk == "low":
                risk = "medium"
        elif humidity_pct > self.HUMIDITY_HIGH and not lockdown:
            issues.append(f"Humidity {humidity_pct:.1f}% is above target range")
            recommendations.append("Remove some water or increase ventilation briefly.")
            if risk == "low":
                risk = "medium"

        # Vision result context
        if vision_label and vision_label not in ("fertile", "unknown", None):
            issues.append(f"Last candling result: {vision_label}")
            if vision_label == "blood_ring":
                recommendations.append("Blood ring detected — remove egg to prevent contamination.")
                risk = _escalate(risk, "medium")
            elif vision_label == "dead_embryo":
                recommendations.append("Dead embryo detected — remove egg promptly.")
                risk = _escalate(risk, "medium")
            elif vision_label == "crack":
                recommendations.append("Cracked shell detected — remove egg to avoid bacterial spread.")
                risk = _escalate(risk, "medium")

        if not issues:
            summary = (
                f"Environment stable at {temperature_c:.1f}°C / {humidity_pct:.1f}% RH — "
                f"within target ranges."
            )
            recommendations.append("Continue current control settings.")
        else:
            summary = " ".join(issues[:2])

        return AIInsight(
            summary=summary,
            confidence="rule-based" if not vision_label else "rule+vision",
            recommendation=" ".join(recommendations) or "Monitor closely.",
            risk_level=risk,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def recent_findings(self, sensor_history: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
        """Return recent trend observations.

        Pass in the last N sensor log rows for real trend calculation,
        or receive canned observations in mock mode.
        """
        if sensor_history and len(sensor_history) >= 2:
            temps = [r["temperature_c"] for r in sensor_history if r.get("temperature_c") is not None]
            hums = [r["humidity_pct"] for r in sensor_history if r.get("humidity_pct") is not None]
            findings = []
            if len(temps) >= 2:
                drift = abs(temps[-1] - temps[0])
                sev = "ok" if drift < 0.5 else "warn"
                findings.append({
                    "title": "Temperature trend",
                    "detail": f"Drift {drift:.2f}°C over last {len(temps)} readings.",
                    "severity": sev,
                })
            if len(hums) >= 2:
                drift = abs(hums[-1] - hums[0])
                sev = "ok" if drift < 3.0 else "warn"
                findings.append({
                    "title": "Humidity trend",
                    "detail": f"Drift {drift:.1f}% over last {len(hums)} readings.",
                    "severity": sev,
                })
            return findings

        return [
            {"title": "Temperature trend", "detail": "Drift <0.3°C over last hour.", "severity": "ok"},
            {"title": "Humidity trend", "detail": "Humidity stable near target ±2%.", "severity": "ok"},
            {"title": "Turn cycle", "detail": "Next scheduled turn window in ~42 minutes.", "severity": "info"},
        ]
