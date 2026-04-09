from __future__ import annotations

from typing import Any


class LLMService:
    """LLM integration surface for future local runtime models."""

    def answer_help_question(self, question: str, device_state: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "configured": False,
            "message": "LLM runtime is not configured yet.",
            "question": question,
            "device_state": device_state or {},
        }

    def explain_status(self, device_state: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "configured": False,
            "message": "Status explanation model is not configured yet.",
            "device_state": device_state or {},
        }
