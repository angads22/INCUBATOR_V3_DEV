from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class AIInsight:
    summary: str
    confidence: str
    recommendation: str
    risk_level: str
    generated_at: str


class AIService:
    """Stub AI service returning structured mock analysis data."""

    def generate_dashboard_insight(self, temperature_c: float, humidity_pct: float) -> AIInsight:
        if temperature_c > 38.0:
            risk = "high"
            recommendation = "Reduce heater duty cycle and check airflow immediately."
        elif temperature_c < 36.5:
            risk = "medium"
            recommendation = "Increase heater output gradually and re-check in 10 minutes."
        else:
            risk = "low"
            recommendation = "Environment is stable. Keep current control settings."

        if humidity_pct < 50:
            recommendation += " Consider adding humidity support to reach target band."

        return AIInsight(
            summary=f"Current readings {temperature_c:.1f}°C and {humidity_pct:.1f}% RH are within monitored range.",
            confidence="mock-medium",
            recommendation=recommendation,
            risk_level=risk,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def recent_findings(self) -> list[dict[str, str]]:
        return [
            {"title": "Heat trend", "detail": "Temperature drift is <0.3°C over last hour.", "severity": "ok"},
            {"title": "Humidity trend", "detail": "Humidity is stable near target ±2%.", "severity": "ok"},
            {"title": "Turn cycle", "detail": "Next scheduled turn window in ~42 minutes.", "severity": "info"},
        ]
